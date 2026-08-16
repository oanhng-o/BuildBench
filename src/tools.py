import os
import sys
import io
import tarfile
import requests
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
import re
from typing import Dict, List, Optional, Tuple
from pprint import pprint
from urllib.parse import urlparse, unquote
import logging
import subprocess
# import docker
import pandas as pd
import json
import argparse
import shutil
from time import time, sleep
import csv

def parse_args(default_values):
    
    parser = argparse.ArgumentParser(description='Multi-LLM Compilation Agent')
    parser.add_argument('--human_in_loop', type=bool, default=False, help='Whether to enable human in the loop')
    parser.add_argument('--model_name', type=str, default=default_values["MODEL_NAME"], choices=['gpt-4o', 'o3-mini', 'gemini-2.5-flash', 'claude-3-7-sonnet-20250219', 'deepseek-v4-flash', 'deepseek-v4-pro','Qwen/Qwen3-235B-A22B-Instruct-2507:together', 'Qwen/Qwen3-Coder-480B-A35B-Instruct:novita'],help='Name of the model')
    parser.add_argument('--max_tokens', type=int, default=default_values["MAX_TOKENS"], help='Maximum tokens as input for the model')
    parser.add_argument('--api_key', type=str, default=default_values['API_KEY'], help='API key')
    parser.add_argument('--temperature', type=float, default=default_values["TEMPERATURE"], help='Temperature for the model')
    parser.add_argument('--operating_system', type=str, default=default_values["OPERATING_SYSTEM"], help='Operating system where the agents will run')
    parser.add_argument('--github_repo', type=str, default=default_values["GITHUB_REPO"], help='GitHub repository to clone and compile')
    parser.add_argument('--optimization_level', type=str, default=default_values["OPTIMIZATION_LEVEL"], help='Optimization level flag')
    parser.add_argument('--save_path', type=str, default=default_values["SAVE_PATH"], help='Path to save the compiled files')
    parser.add_argument('--log_dir', type=str, default=default_values["LOG_DIR"], help='Directory where logs will be saved')
    parser.add_argument('--venv_dir', type=str, default=default_values["VENV_DIR"], help='Virtual environment directory')
    parser.add_argument('--silent', type=bool, default=default_values["SILENT"], help='Whether to print the chat messages or not')
    parser.add_argument('--max_turns', type=int, default=default_values["MAX_TURNS"], help='Maximum number of turns in the conversation')
    parser.add_argument('--cost', type=bool, default=default_values["PRINT_COST"], help='Whether to print the cost of the conversation')
    parser.add_argument('--timeout_bash', type=int, default=default_values["TIMEOUT_BASH"], help='Timeout for bash commands')
    parser.add_argument('--timeout_llm', type=int, default=default_values["TIMEOUT_LLM"], help='Timeout for the LLM')
    parser.add_argument('--sudo_password', type=str, default=default_values['SUDO_PASSWORD'], help='Password for sudo commands')
    parser.add_argument('--if_docker_executor', type=bool, default=default_values["IF_DOCKER_EXECUTOR"], help='Whether to use docker executor')
    parser.add_argument('--docker_image', type=str, default=default_values["DOCKER_IMAGE"], help='Docker image to use for the executor')
    parser.add_argument('--project_dir', type=str, default=default_values['PROJECT_DIR'], help='Project directory')
    parser.add_argument('--auto_build', type=bool, default=default_values['AUTO_BUILD'], help='Whether to use auto build agents')
    parser.add_argument('--agents_number', type=int, default=default_values['AGENTS_NUMBER'], help='Number of agents to use. Default is 2: Executor and Compilation agent. If 3, Installation agent will be used')
    # parser.add_argument('--bash_script', type=str, default=DEFAULT_VALUES['BASH_SCRIPT'], help='Bash script to execute')
    parser.add_argument('--random', type=int, default=default_values['RANDOM'], help='Number of random repos to compile')
    parser.add_argument('--args_save_location', type=str, default=default_values['ARGS_SAVE_LOCATION'],   help='Number of random repos to compile')
    parser.add_argument('--host_project_dir', type=str, default=default_values['HOST_PROJECT_DIR'],  help='Host project directory')
    parser.add_argument('--test', type=bool, default=default_values['TEST'], help='Whether to use the test set')
    parser.add_argument('--image_pull_policy', type=bool, default=default_values['IMAGE_PULL_POLICY'], help='True to always pull the image, False to pull if not present')
    parser.add_argument('--github_token', type=str, default=default_values['GITHUB_TOKEN'], help='Github token to use for higher rate limit')
    parser.add_argument('--starting_index', type=int, default=0, help='Starting index for the repos')
    parser.add_argument('--ending_index', type=int, default=-1, help='ending index for the repos')
    parser.add_argument('--cores', type=int, default=default_values['CORES'], help='How many cores available for each instance')
    parser.add_argument('--k8s_parallelism', type=int, default=default_values['K8S-PARALLELISM'], help='How many parallel instances to run in K8S')
    parser.add_argument('--backoff_limit', type=int, default=default_values['BACKOFF_LIMIT'], help='Backoff limit for K8S jobs')
    parser.add_argument('--refine_times', type=int, default=default_values['REFINE_TIMES'], help='How many times to refine the compilation command generation process')
    # Retrieval flag: defaults based on default_values, can be enabled or disabled explicitly
    parser.add_argument('--retrieval', dest='retrieval', action='store_true', help='Enable retrieval')
    parser.add_argument('--no-retrieval', dest='retrieval', action='store_false', help='Disable retrieval')
    parser.set_defaults(retrieval=default_values['RETRIEVAL'])
    parser.add_argument('--data_path', type=str, default=default_values['DATA_PATH'], help='Path to the data file')
    parser.add_argument("--retrieval_input_path", type=str, default=default_values['RETRIEVAL_INPUT_PATH'], help='Path to the retrieval input file')
    parser.add_argument('--perfect_retrieval', action='store_true', dest='PERFECT_RETRIEVAL', help='Enable perfect retrieval')
    parser.add_argument('--no-perfect_retrieval', dest='PERFECT_RETRIEVAL', action='store_false', help='Disable perfect retrieval')
    parser.add_argument('--RAG_retrieval', dest='RAG_RETRIEVAL', action='store_true', help='Enable RAG retrieval')
    parser.set_defaults(PERFECT_RETRIEVAL=default_values['PERFECT_RETRIEVAL'])
    return parser.parse_args()

def load_file(file_path):
    print("Loading file: ", file_path)
    if file_path.endswith('.csv'):
        return pd.read_csv(file_path)
    elif file_path.endswith('.jsonl'):
        data = []
        with open(file_path, 'r') as file:
            for line in file:
                data.append(json.loads(line))
        return pd.DataFrame(data)
    else:
        raise ValueError("Unsupported file format")

def get_target_github_repos(args, default_values, data_path, github_token=None):
    ### Read the csv file to get the list of GitHub repositories and exclude the ones that are deemed to fail by human verification
    github_repos = load_file(data_path)
    if args.test:
        ### Testing with the test set
        github_repos = github_repos[github_repos['Deem_to_Fail'] != 'Yes']
        github_repos = github_repos[default_values['COLUMN_NAME']].to_list()
    else:
        ### Deploying with the full set
 
        ### TODO: temporary license filters, need to refine and double check what to do with multiple licenses 
        # github_repos = github_repos[github_repos['license_type'] == "permissive"]
        github_repos = github_repos['full_name'].to_list()
        
        if github_token is not None:
            github_repos = [f'https://{github_token}@github.com/' + repo + '.git' for repo in github_repos]    
        else:
            github_repos = ['https://github.com/' + repo + '.git' for repo in github_repos]    
    print("Total number of repos to compile: ", len(github_repos))  
    ### Use args.random to determine the number of repos to compile
    if args.random>0:
        import random
        # Randomly pick given number of repos from the csv file to compile
        github_repos = random.sample(github_repos, args.random)
        print(f'Randomly picking {args.random} repos to compile...')
        ### Make sure the test set is all cloned locally  TODO: May be optional and should be removed later if not needed
        # clone_repository(csv_path=Default_Values['DATA_PATH'], column_name=Default_Values['COLUMN_NAME'], save_path=args.save_path)        
    elif args.random==-1: 
        github_repos = [args.github_repo]
        print(f'Only compiling the repo {args.github_repo}...')
    elif args.random==-2:
        start_index = int(args.starting_index)
        end_index = int(args.ending_index)
    
        github_repos = github_repos[start_index:end_index]
        print(f'Compiling repos from index {start_index} to {end_index}...')
    elif args.random==0:
        print(f'Compiling all repos...')
    else:
        raise ValueError('Invalid random value. It should be greater than 0 to randomly pick the repos or -1 to compile a single repo')
    
    return github_repos

def safe_log(logger, level, message):
    if logger:
        log_func = getattr(logger, level, None)
        if callable(log_func):
            log_func(message)
    else:
        print(message)  # Fallback to print if logger is not defined
        
def read_password(path):
    if os.path.exists(path):
        key = open(path).read().strip()
    else:
        key = None
    if key is None:
        raise ValueError('API Key not found')        
    return key

def get_perfect_retrieval_labels(input_file_path):
    with open(input_file_path, 'r', encoding='utf-8-sig', newline='') as f:
        repo_list = csv.DictReader(f)
        
        perfect_retrieval_labels = {}
        for repo_data in repo_list:

            ### Some repo may not have a build trajectory
            if repo_data['Build Trajectory'] == 'None':
                continue
            ### We also do not include repos that are not compilable
            if repo_data['Compilable?'] != 'Yes':
                continue

            repo_name = repo_data['Repo_Name'].split('/')[1]
            default_branch = repo_data['default_branch']

            ### Here we assume that the start of the trajectory is always the repo url without .git. However, when treating the benchmark, we use the readme path to check with the predicted target link or trajectory. 
            ground_truth_trajectory = repo_data['Build Trajectory']
            # Split into parts, trim whitespace, and append README path to the first element.
            trajectory_parts = [part.strip() for part in ground_truth_trajectory.split(',')]
            trajectory_parts[0] = trajectory_parts[0] + '/blob/' + default_branch + '/' + 'README.md'
            ### Since there may be multiple retrieval target links that could be used, they are seperated by "&&" in the data           
            retrieval_target_link = trajectory_parts[-1]
            perfect_retrieval_labels[repo_name] = retrieval_target_link
    
    return perfect_retrieval_labels
                
                
                
def setup_logger(repo_name, log_dir, experiment_time):
    try:
        logger = logging.getLogger(repo_name)
        logger.setLevel(logging.INFO)

        # Create log file handler
        log_file = os.path.join(log_dir, f'{repo_name}_{experiment_time}.log')
        fh = logging.FileHandler(log_file, mode='w')
        fh.setLevel(logging.DEBUG)

        # Create log file handler for latest.log
        latest_log_file = os.path.join(log_dir, 'latest.log')
        fh_latest = logging.FileHandler(latest_log_file, mode='w')
        fh_latest.setLevel(logging.DEBUG)
        
        # --- Stream Handler (for stdout, visible in kubectl logs) ---
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.DEBUG)
        
        # Create formatter and add it to the handler
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        fh_latest.setFormatter(formatter)

        # Add the handler to the logger
        logger.addHandler(fh)
        logger.addHandler(fh_latest)
        logger.addHandler(sh)
    except Exception as e:
        raise Exception(f"Error setting up logger: {e}")
    # Return the configured logger
    return logger






def write_file(path: str, content: str) -> str:
    with open(path, 'w') as f:
        f.write(content)
    return "File written successfully"


def check_for_compilation(path: str) -> str:
    found_elf = False
    location = None
    for root, _, files in os.walk(path):
        for file in files:
            file_path = os.path.join(root, file)
            extension = file.split('.')[-1]
            if '.' not in file or extension == 'exe':            
                with open(file_path, 'rb') as r:
                    by = r.read(4)
                    if by == b"\x7fELF":
                        found_elf = True
                        location = file_path
                        break
        if found_elf:
            break
    return f'Found a compiled file at {location}' if found_elf else 'No compiled files found'




def search_google(query: str, get_results: int=10) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0'
    }
    response = requests.get(f'https://www.google.com/search?q={query}', headers=headers)
    all_results = []
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        relevant_results = soup.select('.egMi0')[:get_results]
        for relevant_result in relevant_results:
            title = relevant_result.find('h3', class_='zBAuLc l97dzf').get_text()
            link = parse_qs(urlparse(relevant_result.find('a')['href']).query)['q'][0]
            all_results.append({
                'title': title,
                'link': link
            })
    if len(all_results) > 0:
        return all_results
    return "No results found"




def remove_and_copy_directory_by_agents(repo_name: str) -> tuple[int, str]:
    try:
        # Construct the command
        command = (
            f"/bin/sh -c 'if [ -d /app/compiled_repos/{repo_name} ]; then "
            f"rm -rf /app/compiled_repos/{repo_name} && echo \"repo existed and was removed\"; "
            f"fi && cp -r /app/cloned_repos/{repo_name} /app/compiled_repos/'"
        )
        # Execute the command
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        # Return the exit code and output
        return result.returncode, result.stdout + result.stderr
    except Exception as e:
        return 1, str(e)

def remove_and_copy_directory(
    container,  
    source_dir: str,
    destination_dir: str,
    repo_name: str,
) -> Tuple[int, str]:
    """
    Removes `destination_dir` (if it exists) and copies the entire directory
    from `source_dir` to `destination_dir`.

    If `container` is None, performs local filesystem operations.
    Otherwise, executes the command within the given Docker container.

    :param container: Docker container object or None.
    :param source_dir: Source directory path (e.g., /app/cloned_repos/<repo_name>).
    :param destination_dir: Destination directory path (e.g., /app/compiled_repos/<repo_name>).
    :return: A tuple of (exit_code, output_string).
    """
    output_message = ""

    if container is None:
        # Perform the operations on the local filesystem
    
        try:

            # Check if the destination exists; if so, remove it
            if os.path.isdir(destination_dir) and os.path.exists(destination_dir):
                shutil.rmtree(destination_dir)
                output_message += "repo existed and was removed\n"
                os.makedirs(destination_dir, exist_ok=True)            
            else:
                os.mkdir(destination_dir)     
                       
            # Compress the source directory to a tarball
            tar_file_name = f"{repo_name}_archive.tar.gz"
            tar_file_nfs_path = os.path.join(os.path.dirname(source_dir), tar_file_name)
            if not os.path.exists(tar_file_nfs_path):
                compress_cmd = [
                    "tar",
                    "-czf",
                    tar_file_nfs_path,  # the full path to the tarball
                    "-C",
                    os.path.dirname(source_dir),
                    os.path.basename(source_dir)
                ]
                subprocess.run(compress_cmd, check=True)
                output_message += f"Compressed {source_dir} to {tar_file_nfs_path}\n"

            else:
                output_message += f"Tarball {tar_file_nfs_path} already exists"

            # copy the compressed file to parent directory of the destination directory
            shutil.copy2(tar_file_nfs_path,  os.path.dirname(destination_dir))
            output_message += f"Copied {tar_file_nfs_path} to {os.path.dirname(destination_dir)}\n"
            tar_file_dst_path = os.path.join(os.path.dirname(destination_dir), tar_file_name) # in docker
            uncompressed_cmd = [
                "tar",
                "-xzf",
                tar_file_dst_path,
                "-C",
                os.path.dirname(destination_dir)
            ]
            subprocess.run(uncompressed_cmd, check=True)
            output_message += f"Uncompressed {tar_file_dst_path} to {destination_dir}\n"

            # This will throw error, see the notes corresponding with this commit.
            # shutil.copytree(source_dir, destination_dir)
            output_message += f"Copied {source_dir} to {destination_dir}\n"   

            exit_code = 0
                
        except Exception as e:
            exit_code = 1
            output_message += f"Error copying directory: {e}\n"

        return exit_code, output_message

    else:
        print(f"Copying {source_dir} to {destination_dir} in container...")
        # Run commands inside the container
        remove_and_copy_cmd = (
            "/bin/sh -c "
            f"'if [ -d \"{destination_dir}\" ]; then "
            f"    rm -rf \"{destination_dir}\" && echo \"repo existed and was removed\"; "
            f"fi && cp -r \"{source_dir}\" \"{destination_dir}\"'"
        )

        exit_code, output = container.exec_run(
            cmd=remove_and_copy_cmd,
            workdir="/app",
            user='root'
        )
            # Decode if the output is in bytes (common with Docker exec_run)
            
        if isinstance(output, bytes):
            output = output.decode("utf-8").strip()
        else:
            output = str(output).strip()

        output+= 'repo existed and was removed\n'
        # output is typically bytes; caller can decode if needed
        return exit_code, output


def remove_and_copy_directory_wrapper(
    container,
    repo_name: str,
    logger,
    cloned_repos_path: str = "/app/cloned_repos",
    compiled_repos_path: str = "/app/compiled_repos"
):
    """
    Wrapper to remove any existing compiled directory for `repo_name` and then copy
    from the cloned directory. Logs messages before/after the operation and on error.

    :param container: Docker container object or None.
    :param repo_name: Name of the repository folder.
    :param logger: Logger object for info/error messages.
    :param cloned_repos_path: Path to cloned repositories (defaults to /app/cloned_repos).
    :param compiled_repos_path: Path to compiled repositories (defaults to /app/compiled_repos).
    """

    source_dir = os.path.join(cloned_repos_path, repo_name)
    destination_dir = os.path.join(compiled_repos_path, repo_name)
    os.makedirs(destination_dir, exist_ok=True)

    exit_code, output_str = remove_and_copy_directory(
        container=container,
        source_dir=source_dir,
        destination_dir=destination_dir,
        repo_name=repo_name
    )



    # Log if the repository was removed
    if "repo existed and was removed" in output_str:
        logger.info(
            f"{repo_name} already existed in {compiled_repos_path} and was removed before copying."
        )

    # Handle errors or success
    if exit_code != 0:
        logger.error(
            f"Failed to copy cloned repository {repo_name} from {source_dir} to {destination_dir} in container: {output_str}"
        )
        raise Exception(f"Failed to copy directory in container: {output_str}")
    else:
        logger.info(
            f"Copied {repo_name} to {compiled_repos_path} directory in host, "
            f"which is mounted at {destination_dir} in container."
        )

def create_tarball(source_dir: str, tarball_path: str) -> None:
    """
    Create a .tar.gz archive from a source directory.
    
    :param source_dir: Path to the directory you want to archive.
    :param tarball_path: Destination file path (including .tar.gz extension).
    """
    # Check if the repo has a .git folder and remove it if it does to save space
    if os.path.exists(os.path.join(source_dir, '.git')):
        shutil.rmtree(os.path.join(source_dir, '.git'))
        print(f"Removed .git folder from {source_dir} to save space.")
    elif os.path.exists(os.path.join(source_dir, '.github')):
        shutil.rmtree(os.path.join(source_dir, '.github'))
        print(f"Removed .github folder from {source_dir} to save space.")
        
    # Ensure the parent directory of tarball_path exists
    os.makedirs(os.path.dirname(tarball_path), exist_ok=True)
    
    # Open the tar file in write+gzip mode
    with tarfile.open(tarball_path, "w:gz") as tar:
        # The arcname argument sets how the directory appears inside the tar
        tar.add(source_dir, arcname=os.path.basename(source_dir))
    print(f"Created tarball at {tarball_path}")

def extract_tarball_subprocess(tarball_path: str, extract_to_dir: str) -> None:
    """
    Extract a .tar.gz file using a subprocess call to 'tar'.
    (Equivalent to 'tar -xzf tarball_path -C extract_to_dir')
    
    :param tarball_path: The path to the .tar.gz file to extract.
    :param extract_to_dir: The directory where files should be extracted.
    """
    # Ensure the target extract directory exists
    if os.path.exists(extract_to_dir):
        shutil.rmtree(extract_to_dir)

    # Create the directory
    os.makedirs(extract_to_dir, exist_ok=True)
    # Run a subprocess to call the 'tar' command
    # 'z' for gzip, 'x' for extract, 'f' for specifying the file
    # '-C' to change to the extraction directory
    # --no-same-owner: on a bind mount backed by a VM (Docker Desktop) or on NFS
    # with root_squash, restoring uid/gid 0 fails and tar exits 2.
    subprocess.run([
        "tar",
        "-xzf",
        tarball_path,
        "--no-same-owner",
        "-C",
        extract_to_dir
    ], check=True)
    print(f"Extracted {tarball_path} into {extract_to_dir}")
    
    
# def copy_directory_from_container(container:docker.DockerClient, container_path:str, local_path:str) -> None:
#     ### For local compilation, not suitable for K8S implementation
#     bits, stat = container.get_archive(container_path)
#     file_obj = io.BytesIO()
#     for chunk in bits:
#         file_obj.write(chunk)
#     file_obj.seek(0)
#     with tarfile.open(fileobj=file_obj) as tar:
#         def is_within_directory(directory, target):
#             abs_directory = os.path.abspath(directory)
#             abs_target = os.path.abspath(os.path.join(directory, target))
#             return os.path.commonprefix([abs_directory, abs_target]) == abs_directory
#         def safe_extract(tar_obj, path=".", members=None):
#             for member in tar_obj.getmembers():
#                 member_path = os.path.join(path, member.name)
#                 if not is_within_directory(path, member_path):
#                     raise Exception("Attempted Path Traversal in Tar File")
#             tar_obj.extractall(path=path, members=members)
#         safe_extract(tar, path=local_path)
        
def sanitize_k8s_name(name):
    """
    Converts a string to a valid Kubernetes resource name according to RFC 1123.
    - Converts to lowercase.
    - Replaces invalid characters with '-'.
    - Ensures the name starts and ends with an alphanumeric character.
    """
    # Convert to lowercase
    name = name.lower()
    
    # Replace invalid characters with '-'
    name = re.sub(r'[^a-z0-9\-\.]', '-', name)
    
    # Remove leading non-alphanumerics
    name = re.sub(r'^[^a-z0-9]+', '', name)
    
    # Remove trailing non-alphanumerics
    name = re.sub(r'[^a-z0-9]+$', '', name)
    
    # Optionally, ensure the name is within 253 characters
    return name[:253]

def clone_repository(repo_url, save_path, logger=None, init_submodules=True):
    repo_name = repo_url.split('/')[-1].replace('.git', '')
    clone_repo_dir = os.path.join(save_path, repo_name)

    if not os.path.exists(clone_repo_dir):
        try:
            cmd = ["git", "clone", "--depth=1", repo_url, clone_repo_dir]
            max_attempts = 3
            delay = 30  # seconds
            attempt = 1

            while attempt <= max_attempts:
                safe_log(logger, 'info', f"Cloning {repo_url}... (Attempt {attempt}/{max_attempts})")
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                if result.returncode == 0:
                    safe_log(logger, 'info', f"Successfully cloned {repo_url}.")

                    # Initialize and update submodules
                    if init_submodules:
                        submodule_cmd = ["git", "submodule", "update", "--init", "--recursive"]
                        submodule_result = subprocess.run(
                            submodule_cmd, cwd=clone_repo_dir,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE
                        )

                        if submodule_result.returncode == 0:
                            safe_log(logger, 'info', f"Successfully initialized submodules for {repo_url}.")
                        else:
                            safe_log(logger, 'warning', f"Failed to initialize submodules:\n{submodule_result.stderr.decode('utf-8')}")

                    break
                
                else:
                    safe_log(logger, 'warning', f"Failed to clone {repo_url}:\n{result.stderr.decode('utf-8')}")

                    if attempt < max_attempts:
                        safe_log(logger, 'info', f"Retrying in {delay} seconds...")
                        sleep(delay)
                        delay *= 2
                    else:
                        safe_log(logger, 'error', f"Exceeded maximum retry attempts ({max_attempts}). Exiting.")
                        raise Exception(f"Exceeded maximum retry attempts ({max_attempts}). Exiting.")
                attempt += 1

        except Exception as e:
            safe_log(logger, 'error', f"Failed to clone {repo_url}: {e}")
        return clone_repo_dir

    else:
        safe_log(logger, 'info', f"Repository {repo_name} already exists in {save_path}")    
        return clone_repo_dir

def convert_sets_to_lists(obj):
    if isinstance(obj, dict):
        return {k: convert_sets_to_lists(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_sets_to_lists(item) for item in obj]
    elif isinstance(obj, set):
        return list(obj)
    else:
        return obj

def is_valid_url(url: str) -> bool:
    """Validate if the extracted string is a proper URL"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False

## For Testing
if __name__ == '__main__':
    # print(list_files('/home/divij/Desktop/compiled_repos/netdata'))
    # print(read_file('/home/divij/Desktop/compiled_repos/Ventoy/README.md'))
    # print(search_google('exitcode: 1 (execution failed)\nCode output: configure: error: select TLS backend(s) or disable TLS with --without-ssl.'))
    # print(check_for_compilation('/home/divij/Desktop/compiled_repos/redis/_compiled_files'))
    pass