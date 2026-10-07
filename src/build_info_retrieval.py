import os
import re
import json
from typing import Dict, List, Optional, Tuple
from time import time, sleep
import requests
from openai import OpenAI
from anthropic import Anthropic
from google import genai
from google.genai import types

from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import BaseModel
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from tavily import TavilyClient

import env_config

class Extract_Build_Information_and_Links(BaseModel):
    Build_Instructions: str = ''
    External_URLs: list[str] = []
    Internal_Paths: list[str] = []
    

class Extract_Build_Information_and_External_Links(BaseModel):
    Build_Instructions: str = ''
    External_URLs: list[str] = []
        
API_KEY = env_config.get('API_KEY')
TAVILY_API_KEY = env_config.get('TAVILY_API_KEY')
MODEL_NAME = env_config.get("MODEL_NAME")
HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"
GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/'
DEEPSEEK_BASE_URL = env_config.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# Lazy initialization — keys are required at runtime, not import time
if MODEL_NAME:
    if 'claude' in str(MODEL_NAME):
        print("Claude model is in use for retrieval")
    elif 'qwen' in str(MODEL_NAME).lower():
        print("Qwen model is in use for retrieval")
    elif 'gemini' in str(MODEL_NAME).lower():
        print("Gemini model is in use for retrieval")
    elif 'deepseek' in str(MODEL_NAME).lower():
        print("DeepSeek model is in use for retrieval")
    else:
        print(f"{MODEL_NAME} model is in use for retrieval")

tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

def llm_response_structured(model_name, response_format, system_prompt, input):
    if 'claude' in str(model_name).lower():
        if response_format == Extract_Build_Information_and_Links:
                system_prompt +=  "Output in JSON format with keys: “Build_Instructions” (str), “External_URLs” (list [str]), and “Internal_Paths” (list [str])"
        elif response_format == Extract_Build_Information_and_External_Links:
                system_prompt +=  "Output in JSON format with keys: “Build_Instructions” (str), and “External_URLs” (list [str])"
                
        client = Anthropic(
                api_key = API_KEY,                
            )
        response = client.messages.create(
            model=model_name,
            max_tokens=2000,
            # temperature=1,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": input
                        }
                    ]
                }
            ]
        )
        response = ''.join([block.text for block in response.content if block.type == "text"])
        # print("response from Anthropic", response)
        if response.startswith("```json"):
            response = response[7:-3]
        output = json.loads(response)
        output = response_format(**output)
        
    elif 'gpt' in str(model_name).lower() or 'o3' in str(model_name).lower() or 'o4' in str(model_name).lower():
        client = OpenAI(
            api_key=API_KEY,
        )
        response = client.responses.parse(
            model=model_name,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input},
            ],
            text_format=response_format,
        )
        output = response.output_parsed
        
    elif 'qwen' in str(model_name).lower():
        client = OpenAI(api_key=API_KEY,
                base_url=HUGGINGFACE_BASE_URL)
        
        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input}
            ],
            response_format={
                "type": "json_schema",
                "schema": response_format.model_json_schema(),
            },
        )
        output = json.loads(completion.choices[0].message.content)
        output = response_format(**output)
        
    elif 'gemini' in str(model_name).lower():
        # client = OpenAI(
        #     api_key=API_KEY,
        #     base_url=GEMINI_BASE_URL,
        # )
        # response = client.responses.parse(
        #     model=MODEL_NAME,
        #     input=[
        #         {"role": "system", "content": system_prompt},
        #         {"role": "user", "content": input},
        #     ],
        #     text_format=response_format,
        # )
        # output = response.output_parsed
        client = genai.Client(api_key=API_KEY)
        response = client.models.generate_content(
        model=model_name,
        contents=system_prompt + input,
        config={
            "response_mime_type": "application/json",
            "response_schema": response_format,
            },
        )
        output = response.parsed
    elif 'deepseek' in str(model_name).lower():
        ### DeepSeek's OpenAI-compatible API offers JSON mode but not json_schema,
        ### so the shape goes in the prompt and is validated on the way out. JSON
        ### mode also requires the word "json" to appear in the prompt.
        system_prompt += (
            "\nRespond with json only, matching this schema: "
            + json.dumps(response_format.model_json_schema())
        )
        client = OpenAI(
            api_key=API_KEY, 
            base_url=DEEPSEEK_BASE_URL,
            default_headers= {
               "User-Agent": "claude-cli/1.0.0 (external, cli)",
               "X-App": "cli",
           })
        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input}
            ],
            response_format={"type": "json_object"}
        )
        response = completion.choices[0].message.content
        if response.startswith("```json"):
            response = response[7:-3]
        output = response_format(**json.loads(response))

    else:
        print(f"{model_name} is an unsupported model type. Please use a supported model.")
        output = response_format()

    return output


def search_online_using_tavily(repo_full_name: str) -> str:
    '''
    Search online using tavily, returning only the content of the url from the top 1 result
    
    Parameters:
        repo_full_name (str): The full name of the repository (e.g., "owner/repo").
    
    Returns:
        str: The content of the URL from the top result.
    '''
    query = f'How to build the {repo_full_name} repository from Github from source?'
    response = tavily_client.search(
        query=query,
        max_results=1
    )
    url = response['results'][0]['url']
    content = asyncio.run(get_html_content_in_markdown(url))
    return content


async def get_html_content_in_markdown(url: str, timeout: int = 300000) -> str:
    """
    Retrieves the fully rendered HTML content of a given URL.
    Waits until the client-side redirection (if any) is complete.
    
    Parameters:
        url (str): The URL to navigate to.
        timeout (int): Timeout in milliseconds for navigation and waiting.
        
    Returns:
        str: The HTML content of the page if successful, or an empty string on failure.
    """
    # response = requests.get(url,  allow_redirects=True)
    # html_content = None    
    # if response.status_code == 200:
    #     # soup = BeautifulSoup(response.text, 'html.parser')
    #     # paragraphs = soup.find_all('p')
    #     # content = ' '.join([p.text for p in paragraphs])
    #     html_content = response.text
    # else:
    #     print("****** USING PLAYWRIGHT TO GET HTML CONTENT ******")
    #     async with async_playwright() as p:
    #         # Launch the browser
    #         browser = await p.chromium.launch(headless=True)
    #         # Set a user agent to mimic a typical desktop browser
    #         user_agent = (
    #             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    #             "AppleWebKit/537.36 (KHTML, like Gecko) "
    #             "Chrome/115.0.0.0 Safari/537.36"
    #         )
    #         # Create a new browser context with the custom user agent
    #         context = await browser.new_context(user_agent=user_agent)
    #         page = await context.new_page()
    #         try:
    #             # Navigate to the URL with DOMContentLoaded event as a checkpoint.
    #             response = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    #             if response is None:
    #                 print("Navigation failed: No response received.")
    #                 return ""
                
    #             # Allow a brief pause for any client-side redirection to occur.
    #             await asyncio.sleep(1)
                
    #             # Wait until the URL reflects the expected path.
    #             await page.wait_for_function(
    #                 "window.location.href.includes('/en/concepts/installation')",
    #                 timeout=timeout
    #             )
    #             # Optionally, wait until network activity is idle.
    #             await page.wait_for_load_state("networkidle", timeout=timeout)
                
    #             html_content = await page.content()
    #         except TimeoutError as e:
    #             print("Page navigation or redirection timed out:", e)
    #         finally:
    #             await context.close()
    #             await browser.close()
        
    # if html_content is None:
    #     print(f"Error accessing the external link {url}")
    #     html_content = "No content found due to error"

    # # Convert HTML to Markdown
    # markdown = md(html_content)
    # return markdown

    html_content = ""
    final_url = url

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        try:
            # Navigate and wait for network to be idle.
            response = await page.goto(url, timeout=timeout, wait_until="networkidle")
            if response is None:
                print(f"No response from {url}")
            else:
                final_url = page.url

            # Sometimes networkidle isn't enough; ensure document is fully loaded.
            try:
                await page.wait_for_function(
                    "document.readyState === 'complete'",
                    timeout=timeout
                )
            except PlaywrightTimeout:
                # if it times out, we’ll just grab whatever we have
                pass

            html_content = await page.content()

        except PlaywrightTimeout as e:
            print("⏳ Page load timed out:", e)
        finally:
            await context.close()
            await browser.close()

    if not html_content:
        print(f"⚠️ Failed to retrieve content for {url}")
        return ""

    # Convert to Markdown
    md = markdownify(html_content, heading_style="ATX")
    print(f"✅ Fetched and converted: {final_url}")
    return md



def list_files(path: str) -> list[str]:
    """
    List files in a Github repository directory, excluding hidden files and directories.
    """
    def remove_elements(lst, remove_list):
        return [value for value in lst if value.lower() not in remove_list]
    
    return remove_elements(
        lst = os.listdir(path),
        remove_list = [
            '.git',
            '.github',
            '.gitmodules',
            'tests',
            'license',
            'changelog.md',
            'redistributed.md',
            '.dockerignore',
            'dockerfile',
            'docker-compose.yml',
        ]
    )


def find_files(path: str, extension: str) -> list[str]:
    """
    Find files with a specific extension in a directory and its subdirectories.
    """
    found_files = []
    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(extension):
                found_files.append(os.path.join(root, file))
    return found_files

def is_url(link: str) -> bool:
    """
    Determine if a given string is a valid URL.
    """
    parsed = urlparse(link)
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def extract_links(text: str) -> Dict[str, List[str]]:
    """
    Extract external and internal links from the text based on specified patterns.
    """
    patterns = {
        'external': r'\[external\]\s*\[(https?://[^\]]+)\]',
        'internal': r'\[internal\]\s*\[([^\]]+)\]'
    }
    return {key: list(set(re.findall(pattern, text, re.IGNORECASE))) 
            for key, pattern in patterns.items()}

def summarize_text(text: str, system_prompt = None, timeout_min: int = 0, refine: bool = False, response_format = Extract_Build_Information_and_Links, final_refine = False, readme_path = None) -> BaseModel:
    if readme_path is not None:
        readme_dir = os.path.dirname(os.path.abspath(readme_path))
    else:
        readme_dir = None
        
    base_system_prompt = """
        You are an expert at extracting only the most relevant information (such as build instructions, dependency requirements) for building a repository from source for Linux based on provided documentation. Oftentimes, the steps of compilation or building from source for Linux should have already been included in the this file, otherwise, they may be stored in one other local files or external links. Please do not include anything that's not directly related to building from source like community support, License, Developer/API Documentation, Contribution, installation instructions for other use cases (such as Python etc.) or other irrelevant information. 

        """
    if system_prompt is None:
        ### By default, summarizing README if the system prompt is not provided
        system_prompt = base_system_prompt+f""" 
        Additionally, identify up to three external links and up to three internal links within the documentation, following the above objective.
        For external URLs, extract the full url.
        For internal file paths, use {readme_dir} as the base path to complete the partial paths. Besides, refine the internal paths to ensure they are valid paths. For example, the /docs/HowToGuides/GettingStarted.md#installing-dependencies should be completed as {readme_dir}/docs/HowToGuides/GettingStarted.md, as the section name after # would interfere with the validity of the path.
        
        If you determine that there is no information directly related to building from source on Ubuntu like community support, License, Contribution, installation instructions for other use cases (such as Python etc.) or other irrelevant information, simply fill the field of "Build_Instructions" with "No build instructions found" but still try to find useful urls or internal links for External_URLs and Internal_Paths.
        """ 
        # """
        # Include these links in your output by wrapping them in the following format to denote their type:

        # ###[external][URL]###
        # ###[internal][URL or Path]###
        # """
        if refine:
            system_prompt += """
            You are previously given this task, and we extracted the texts from the external and internal links you provided. Now, based on the texts contained in the links, you should refine your output. If you think the previously extracted links are good enough and the build from source instruction has been included in the response, you can say: 'No need to refine the output' in “Build_Instructions” field, and leave the rest of fields empty.
            
            """
        if final_refine:
            system_prompt = base_system_prompt


    try:
        output = llm_response_structured(
            model_name=MODEL_NAME,
            response_format=response_format,
            system_prompt=system_prompt,
            input=text
        )
        if not isinstance(output, response_format):
            raise ValueError(f"Output is not of type {response_format.__name__}. Output: {output}")

    except Exception as e:
        print('When summarizing text:', e)
        output = response_format()
    
    return output

def validate_internal_path(readme_path:str, path: str) -> str:
    """
    Validate the internal path.
    """
    ### NOTE: This is a temporary fix to validate the internal path
    if path[0] == '/':
        path = path[1:]
    readme_dir = os.path.dirname(os.path.abspath(readme_path))
    abs_path = os.path.join(readme_dir, path)
    abs_path = os.path.normpath(abs_path)
    if os.path.exists(abs_path):
        return abs_path
    else:
        print(f"Validated Path {abs_path} from original path {path} not found, returning original path")
        return path

# def extract_and_process_internal_links(readme_path: str, links: dict, text: str = None) -> Dict[str, List[str]]:
#     """
#     process internal links.
#     """
#     # links = extract_links(text)
    
#     internal_links = links.get('internal', [])
    
#     for link in internal_links:
#         if is_url(link):
#             links['external'].append(link)
#             internal_links.remove(link)
#         else:
#             abs_path = validate_internal_path(readme_path, link)
#             if os.path.exists(abs_path):
#                 if os.path.isdir(abs_path):
#                     internal_links.remove(link)
#                     print("The path is a directory, removing it from the internal links")
#                 else:
#                     internal_links.remove(link)
#                     internal_links.append(abs_path)
#                     print("The validated path is correct, replacing the original path with the validated path")
#             else:
#                 print(f"[Path not found] {abs_path}")
#                 # resolved.append(f"[Path not found] {abs_path}")

#     return {
#         'external': links.get('external', []),
#         'internal': internal_links
#     }

def refine_links(readme_content, refine_times = 3, readme_path=None) -> List[Dict[str, str]]:
    """
    Refine the links in the README file by summarizing the content of the external and internal links.
    """
    readme_summary = readme_content
    refine = False
    for i in range(refine_times):
        print("Refining the links for the", i+1, "time")
        if i >= 1:
            refine = True
            
        structured_readme_content_and_links = summarize_text(text=readme_summary, refine=refine, response_format = Extract_Build_Information_and_Links, readme_path=readme_path)
        # link_dict = extract_and_process_links(text = structured_readme_content_and_links, readme_path = readme_path)
        # external_links = link_dict['external']
        # internal_links = link_dict['internal']  
        
        ### Update the readme_summary with generated build instructions     
        print("structured_readme_content_and_links", structured_readme_content_and_links)
        # Safely extract build instructions
        readme_summary = getattr(structured_readme_content_and_links, 'Build_Instructions', '')
        print("readme_summary", readme_summary)
        if not isinstance(readme_summary, str):
            readme_summary = str(readme_summary) if readme_summary is not None else ''

        # Safely extract external links list
        external_links = getattr(structured_readme_content_and_links, 'External_URLs', [])
        if not isinstance(external_links, list):
            external_links = list(external_links) if external_links else []

        # Safely extract internal paths list
        internal_links = getattr(structured_readme_content_and_links, 'Internal_Paths', [])
        if not isinstance(internal_links, list):
            internal_links = list(internal_links) if internal_links else []
            
        link_dict = {
            'external': external_links,
            'internal': internal_links,
        }
        ### Validate internal path
        # link_dict = extract_and_process_internal_links(readme_path = readme_path, links = link_dict)
        
        ### Summarize the external links
        for external_link in external_links:
            try:
                structured_external_summary = asyncio.run(summarize_link(external_link))
                ### Extract the summary from the external link
                print(f"structured_external_summary for link {external_link}: ", structured_external_summary)
                print("*"*10)
                external_summary = structured_external_summary.Build_Instructions
                readme_summary += f"External link: {external_link}\n\n Extracted information: {external_summary} \n\n "
                readme_summary += f"Additional_External_Links: {structured_external_summary.External_URLs}\n\n"
            except Exception as e:
                print(f"Error summarizing external link {external_link}: {e}")
                continue
        ### Summarize the internal links
        for internal_link in internal_links:
            try:
                sturctured_internal_summary = summarize_text(text = read_file(internal_link), response_format=Extract_Build_Information_and_Links)
                print(f"sturctured_internal_summary for link {internal_link}: ", sturctured_internal_summary)
                print("*"*10)
                internal_summary = sturctured_internal_summary.Build_Instructions
                readme_summary += f"Internal link: {internal_link}\n Extracted information: {internal_summary}\n\n"
                readme_summary += f"Additional_External_Links: {sturctured_internal_summary.External_URLs}. Additional_Internal_Links:{sturctured_internal_summary.Internal_Paths}\n\n"
            except Exception as e:
                print(f"Error summarizing internal link {internal_link}: {e}")
                continue

        print("Summarized content after refining the links for the", i+1, "time")
        print("*"*50)
        print(readme_summary)
        print("*"*50)
    
    readme_summary = summarize_text(text=readme_summary, refine=False, final_refine=False, response_format = Extract_Build_Information_and_Links)
        
    return readme_content, readme_summary, link_dict

async def summarize_link(url: str) -> str:
    markdown_html_content = await get_html_content_in_markdown(url)
        
    try:
        response = summarize_text(
            text=markdown_html_content,
            system_prompt = f"""
            You are an expert at extracting only the relevant information (such as build instructions, dependency requirements) needed to build a C/C++ repository from source on Linux. Your output should include only content directly related to the build-from-source process. Exclude any information or links related to community support, licensing, developer/API documentation, contribution guidelines, or installation instructions for alternative use cases (such as Python, R, CLI, etc.). When extracting links, include only those directly related to building from source. If any link is partial, complete it using the base URL {url}.

            """
            
            # """
            # Include these links in your output by wrapping them in the following format to denote their type:

            # ###[external][URL]###
            # """,
            ,
            response_format = Extract_Build_Information_and_External_Links 
        )
        # print("*"*50)
        # print(response)
        # print("*"*50)
        return response

    except Exception as e:
        print(f"Error summarizing link: {e}")
        return "Error summarizing link"


def get_build_dict(repo_dir):
    files = os.listdir(repo_dir) # List of files in the repo
    print("files in the repo", files)
    build_tools_dict = {}

    build_systems = {"make": ["makefile"],
                    "cmake": ["cmakelists.txt"],
                    #  "travisci": [".travis.yml"],
                    #  "circleci": ["config.yml"],
                    #  "rake": ["rakefile"],
                    # "sln": [".sln"],
                    "autoconf": ["configure"],
                    # "java": ["build.gradle", "gradlew", "pom.xml"],
                    "ninja": ["ninja", "build.ninja"],
                    "bootstrap": ["bootstrap"],
                    'meson': ['meson.build'],
                    'readme': ['readme.md', 'readme.txt', 'readme.rst'],
                    'install': ['install', 'install.md', 'install.txt'],
                    'build': ['build.md', 'build.txt']
                    }    
    
    # Check if the file is a build file, if so, save the file name in the build_tools_dict
    for fname in files:
        for build_tool, file_keywords in build_systems.items():
            for file_keyword in file_keywords:
                if file_keyword in fname.strip().lower():
                    if build_tools_dict.get(build_tool) is None:
                        build_tools_dict.update({build_tool: [fname]})
                    else:
                        build_tools_dict[build_tool].append(fname)
                     # Save the build tool and the file name in the build_tools_dict
                
                # Check if the file is a readme file, if so, read the content      
    return build_tools_dict

def get_readme_path(build_tools_dict = None, repo_dir = None):
    if build_tools_dict is not None and build_tools_dict.get('readme') is not None:
        readme_files = build_tools_dict['readme'] 
    else:
        readme_files = find_files(repo_dir, '.md')
        
    if len(readme_files) == 0:
        readme_full_path = None
        print("Exception: No readme file found")
        print("But we will still continue with the process")
    else:
        readme_path = None
        for file in readme_files:
            if 'readme.md' == file.lower():
                readme_path = file
                break
        
        if readme_path != None:
            readme_full_path = os.path.join(repo_dir, readme_path)
        else:
            readme_path = readme_files[0]
            readme_full_path = os.path.join(repo_dir, readme_path)
            
    return readme_full_path

def retrive_env_setup_info(repo_dir, refine_times = 1, refine=True):
    ### This function will be used to retreive the information about the compilation environment setup
    ### Specifically, it will detect if certain files like configure, Makefile, etc. are present in the repo
    ### and return the information about the compilation environment setup
    
    ### Part of credit goes to https://github.com/Assemblage-Dataset/Assemblage/blob/main/assemblage/analyze/analyze.py for this function
    
    ### Check if the repo directory exists
    if not os.path.exists(repo_dir):
        raise FileNotFoundError(f"Directory {repo_dir} does not exist, when trying to retrieve the environment setup information.")
    
    readme_content = None
    readme_path = None
    detailed_compilation_instructions = ""
    link_dict = None
    build_tools_dict = get_build_dict(repo_dir)

    if len(build_tools_dict) == 0:
        build_tools_dict = None

    readme_full_path = get_readme_path(build_tools_dict, repo_dir)
    if readme_full_path is None:
        readme_content = "No readme file found"
        print("Warning: No readme file found, readme content is set to None")
    else:
        readme_content = read_file(readme_full_path) # Get the content of the README file

    if refine:
        readme_content, detailed_compilation_instructions, link_dict = refine_links(readme_content, refine_times = refine_times, readme_path = readme_full_path)    
    else:
        link_dict = None
        detailed_compilation_instructions = ""
        readme_path = readme_full_path
    
    if readme_path is None:
        readme_path = "placeholder readme path"  # Assign a default value or handle appropriately
    return build_tools_dict, readme_content, readme_path, detailed_compilation_instructions, link_dict

def read_file(file_path:str) -> str:
    """
    Reads the content of a file and returns it as a string,
    preserving all hyperlinks.

    Args:
        file_path (str): The file path to the file.

    Returns:
        str: The content of the file.

    Raises:
        FileNotFoundError: If the file does not exist at the given path.
        IOError: If there is an error reading the file.
    """

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"The file '{file_path}' does not exist.")

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as file:
            content = file.read()
        return content
    except IOError as e:
        raise IOError(f"An error occurred while reading '{file_path}': {e}")

    
    
    
## For Testing
if __name__ == '__main__':
    # print(list_files('/home/divij/Desktop/compiled_repos/netdata'))
    # print(read_file('/home/divij/Desktop/compiled_repos/Ventoy/README.md'))
    # print(search_google('exitcode: 1 (execution failed)\nCode output: configure: error: select TLS backend(s) or disable TLS with --without-ssl.'))
    # print(check_for_compilation('/home/divij/Desktop/compiled_repos/redis/_compiled_files'))


    readme_path = "cloned_repos/openssl"
    build_tools_dict = get_build_dict(readme_path)
    print(build_tools_dict)
    if len(build_tools_dict) == 0:
        build_tools_dict = None

    readme_full_path = get_readme_path(build_tools_dict, repo_dir=readme_path)
    print(readme_full_path)
    readme_content = read_file(readme_full_path) # Get the content of the README file
    print(readme_content)
    # exit()
    build_tools_dict, readme_content, readme_path, detailed_compilation_instructions, link_dict = retrive_env_setup_info(readme_path, refine_times = 3)
    # print("build_tools_dict", build_tools_dict)
    # print("readme_content", readme_content)
    print("readme_path", readme_path)
    print("*"*50)
    print("detailed_compilation_instructions", detailed_compilation_instructions)
    print("link_dict", link_dict)
    # print(validate_internal_path("cloned_repos/swift/README.md", "/docs/HowToGuides/GettingStarted.md"))