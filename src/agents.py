import os
os.environ['USER_AGENT'] = 'myagent'
import tempfile
import shutil
import subprocess
import stat

import env_config
import langfuse_tracing as tracing

from autogen import runtime_logging
from autogen import ConversableAgent, GroupChat, GroupChatManager, register_function, AssistantAgent, UserProxyAgent
from autogen.code_utils import create_virtual_env
from autogen.agentchat.contrib.capabilities import transform_messages, transforms
from autogen.coding import LocalCommandLineCodeExecutor, DockerCommandLineCodeExecutor

from bash_executor import BashCodeExecutor
from tools import remove_and_copy_directory_by_agents
from prompts import Prompts
from build_info_retrieval import read_file,get_html_content_in_markdown,list_files, validate_internal_path, search_online_using_tavily
# from validation import validation_pipeline_by_agent
# from langchain_community.document_loaders import WebBaseLoader
# from langchain.text_splitter import RecursiveCharacterTextSplitter



class Agent:
    def __init__(self, human_in_loop, model_name, api_key,  sudo_password ,operating_system ="Ubuntu",  max_turns = 10, max_tokens = 30000, silent = True, temperature = 0,if_docker_executor=False, docker_image="ubuntu:20.04", timeout_bash=300, timeout_llm=60, agents_number=2, retrieval = True, perfect_retrieval = False, RAG_retrieval = False):
        # super().__init__(human_in_loop,
        # model_name,
        # max_tokens,
        # api_key,
        # operating_system,
        # silent,
        # max_turns,
        # temperature,
        # sudo_password)
        self.human_in_loop = human_in_loop
        if ":" not in model_name:
            self.model_name = model_name
        else:
            self.model_name = model_name.split(":")[0]
        self.max_tokens = max_tokens
        self.api_key = api_key
        self.operating_system = operating_system
        self.silent = silent
        if max_turns is not None and max_turns != 'None':
            self.max_turns = int(max_turns)
        else:
            self.max_turns = None
        self.temperature = temperature
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sudo_password = sudo_password
        
        self.if_docker_executor = if_docker_executor
        self.docker_image = docker_image
        self.timeout_bash = timeout_bash
        self.timeout_llm = timeout_llm
        self.agents_number = agents_number
        self.retrieval = retrieval
        self.perfect_retrieval = perfect_retrieval
        self.RAG_retrieval = RAG_retrieval

        self.llm_config = {
            'cache_seed': 41,
            'model': self.model_name,
            'api_key': self.api_key,
            # 'temperature': self.temperature,
            'timeout': self.timeout_llm,
            # "max_retries": 5, ### To avoid the error: "Error: 429 Quota exceeded"
        }
        if 'claude' in self.model_name.lower():
            print("Using Claude model for compilation")
            self.llm_config['api_type'] = "anthropic"
        elif 'qwen' in self.model_name.lower():
            print("Using Qwen model for compilation")
            self.llm_config['base_url'] = "https://router.huggingface.co/v1"
        elif 'gemini' in self.model_name.lower():
            print("Using Gemini model for compilation")
            self.llm_config['base_url'] = "https://generativelanguage.googleapis.com/v1beta/openai/"
        elif 'deepseek' in self.model_name.lower():
            print("Using DeepSeek model for compilation")
            ### OpenAI-compatible endpoint; override for a proxy or a self-hosted deployment
            self.llm_config['base_url'] = env_config.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        ### AutoGen prices a run from its own hardcoded table (OAI_PRICE1K), which
        ### only covers OpenAI/Anthropic ids. Anything else logs "Model X is not
        ### found. The cost will be 0" and reports zero. MODEL_PRICE supplies the
        ### rate as "<prompt>,<completion>" per 1K tokens, so a new provider or a
        ### price change needs no code edit.
        model_price = env_config.get("MODEL_PRICE")
        if model_price:
            self.llm_config['price'] = [float(p) for p in model_price.split(',')]
            print(f"Using MODEL_PRICE {self.llm_config['price']} per 1K tokens for cost reporting")
        elif self.model_name == 'o3-mini':
            print("Using O3-mini model for compilation")
            self.llm_config['price'] =  [0.0011, 0.0044]
        elif self.model_name == 'gpt-4.1':
            print("Using GPT-4.1 model for compilation")
            self.llm_config['price'] = [0.002, 0.008]
        elif self.model_name == 'gpt-4o':
            print("Using GPT-4o model for compilation")
            self.llm_config['price'] = [0.0025, 0.01]
        
        print("Model used to initialize the agent: ", self.model_name)
        
        assert type(self.llm_config) == dict, 'LLM config should be a dictionary'

    def _create_code_executor(self, if_docker=False, docker_image="ubuntu:20.04"):
        # ==========================
        # Reference: Autogen documentation
        # https://microsoft.github.io/autogen/docs/tutorial/code-executors/#local-execution
        
        # This creates a docker executor for better isolation and security.
        # ==========================
            
        # Create a temporary directory to store the code files.
        if not if_docker:
            print("Using local executor")
            # Create a local command line code executor.
            executor = BashCodeExecutor(
            timeout = int(self.timeout_bash),
            # virtual_env_context = create_virtual_env(self.venv_dir),
            # work_dir = self.temp_dir.name
            )
        # Create a Docker command line code executor.
        else:
            print("Using docker executor with image: ", docker_image)
            executor = DockerCommandLineCodeExecutor(
                image=docker_image,  # Execute code using the given docker image name.
                container_name='autogen_docker_executor',  # Name of the container.
                timeout=self.timeout_bash,  # Timeout for each code execution in seconds.
                work_dir=self.temp_dir.name,  # Use the temporary directory to store the code files.
                # work_dir=self.project_dir,  # Use the temporary directory to store the code files.
                # bind_dir=self.project_dir,
            )
        return executor

    def _register_functions(self):
        # register_function(
        #     validation_pipeline_by_agent,
        #     caller = self.compilation_agent,
        #     executor = self.execution_agent,
        #     name = 'validation_pipeline',
        #     description = 'Validate the compilation process by calculating the function numbers extracted from binaries in comparison to those from the source code',
        # )
        register_function(
            remove_and_copy_directory_by_agents, 
            caller=self.compilation_agent,
            executor=self.execution_agent_no_termination,
            name='remove_and_copy_directory',
            description='Remove the directory from /app/compiled_repos/(repo_name) and copy a clean directory from /app/cloned_repos/(repo_name) to the same location',
        )
        
    def _initiate_agents(self, prompts):
        agents_created = []
        print("Initializing agents...")
        executor = self._create_code_executor(self.if_docker_executor, self.docker_image)
        # print("Human input mode: ", 'ALWAYS' if self.human_in_loop == True else 'NEVER')
        if not self.auto_build:
            ### Using our own agents
            print("Using our own agents")
            self.execution_agent = UserProxyAgent(
                name = 'Execution',
                system_message = prompts.executor_system_message(), 
                llm_config = self.llm_config,
                description = 'Responsible for executing bash commands or functional calling and conducts the process of Github repository compilation',
                is_termination_msg = lambda msg: 'terminate' in msg['content'].lower() or 'Happy coding!' in msg['content'],
                human_input_mode = 'NEVER',    # ALWAYS, TERMINATE, NEVER
                code_execution_config = {'executor': executor},
            )
            self.compilation_agent = AssistantAgent(
                name = 'Bash_Command_Generator',
                system_message = prompts.compilation_bash_command_generator_system_message(), 
                llm_config = self.llm_config,
                description = 'Responsible for generating bash commands based on retrieved build instructions and error handling for the compilation process but no execution',
                # is_termination_msg = lambda msg: msg.get('content') is not None and 'terminate' in msg['content'].lower(),
                human_input_mode = 'NEVER', 
            )
            agents_created.extend([ self.compilation_agent, self.execution_agent]) 
            
            if self.agents_number == 3:       
                ### Overriding the previous agent to remove the termination msg, which causes error when tool calling.     
                self.execution_agent_no_termination = UserProxyAgent(
                    name = 'Execution',
                    system_message = prompts.executor_system_message(), 
                    llm_config = self.llm_config,
                    description = 'Responsible for executing bash commands or functional calling and conducts the process of Github repository compilation',
                    # is_termination_msg = lambda msg: 'terminate' in msg['content'].lower() or 'happy' in msg['content'].lower(),
                    human_input_mode = 'NEVER',    # ALWAYS, TERMINATE, NEVER
                    code_execution_config = {'executor': executor},
                )
                self.retreiver_agent = AssistantAgent(
                    name = 'Build_Instructions_Retriever',
                    system_message = prompts.retriever_system_message(), 
                    llm_config = self.llm_config,
                    description = 'Responsible for retrieving necessary build instructions by reading files and searching the web, should be called first, and should be called if the retrieved results is not fulfilling the requirements well. ',
                    is_termination_msg = lambda msg: msg.get('content') is not None and 'terminate' in msg['content'].lower(),
                    human_input_mode = 'NEVER', 
                    # code_execution_config = {
                    #     'executor': executor
                    # },
                )
                self.retreiver_agent.register_for_llm(name="read_file", description="Read contents from a local file")(read_file)
                self.retreiver_agent.register_for_llm(name="get_html_content_in_markdown", description="Read contents from a external url in Markdown format")(get_html_content_in_markdown)
                # self.retreiver_agent.register_for_llm(name="validate_internal_path", description="Validate an internal file path from a project root directory.")(validate_internal_path)        
                self.retreiver_agent.register_for_llm(name="list_files", description="List all files in a directory")(list_files)
                self.retreiver_agent.register_for_llm(name="search_online_using_tavily", description="Search online by writing a query")(search_online_using_tavily)

                self.execution_agent_no_termination.register_for_execution(name="read_file")(read_file)
                self.execution_agent_no_termination.register_for_execution(name="get_html_content_in_markdown")(get_html_content_in_markdown)
                # self.execution_agent.register_for_execution(name="validate_internal_path")(validate_internal_path)
                self.execution_agent_no_termination.register_for_execution(name="list_files")(list_files)
                self.execution_agent_no_termination.register_for_execution(name="search_online_using_tavily")(search_online_using_tavily)
                

                agents_created.append(self.retreiver_agent)
                agents_created.extend([ self.execution_agent_no_termination]) 


            
            print("Agents created: ", [agent.name for agent in agents_created])

        else:
            print("Using auto build agents")
            raise NotImplementedError("Auto build agents are not implemented yet")
            
        return agents_created    

    def _clone_repo(self):
        if not self.silent:
            print(f'Cloning the GitHub repository, {self.github_repo} at {self.compiled_dir}...')

        def remove_readonly(func, path, _):
            os.chmod(path, stat.S_IWRITE)
            func(path)
        
        shutil.rmtree(self.compiled_dir, onerror=remove_readonly) if os.path.exists(self.compiled_dir) else None
        command = f'git clone --recursive "{self.github_repo}" "{self.compiled_dir}"'
        output = subprocess.run(
            command,
            shell = True,
            capture_output = True,
            text = True
        )
        if output.returncode != 0:
            command = f'git clone "{self.github_repo}" "{self.compiled_dir}"'
            output = subprocess.run(
                command,
                shell = True,
                capture_output = True,
                text = True
            )
            if output.returncode != 0:
                raise Exception(f'Failed to clone the repo: {output.stderr.strip()}')

    def _initiate_context_handling(self, agents_created = []):
        context_handling = transform_messages.TransformMessages(
            transforms = [
                transforms.MessageTokenLimiter(
                    min_tokens = self.max_tokens,
                    max_tokens = self.max_tokens,
                    model = self.model_name
                )
            ]
        )        
        
        if len(agents_created) == 0:
            raise Exception('No agents created yet. Please create agents first.')
        
        for agent in agents_created:
            context_handling.add_to_agent(agent)
        
        return context_handling, agents_created

    def _initiate_agents_and_context_handling(self, prompts):
        agents_created = self._initiate_agents(prompts=prompts)
        context_handling, agents_created= self._initiate_context_handling(agents_created)
        # self._register_functions()
        return agents_created, context_handling
    
    def compile_repo(self, repo_url, optimization_level, compiled_dir, log_dir, print_cost,  autobuild=False, cores=12, refine_times = 1 ):
        self.github_repo = repo_url
        self.github_repo_name = self.github_repo.split('/')[-1].split('\\')[-1].replace('.git', '')
        self.optimization_level = optimization_level
        
        self.compiled_dir = compiled_dir
        assert os.path.exists(self.compiled_dir) == True, f"Save directory {self.compiled_dir} does not exist"
        
        self.compiled_dir = os.path.join(self.compiled_dir, self.github_repo_name)
        # self.compiled_save_path = os.path.join(str(self.clone_dir)+'_compiled_files')
        # self.compiled_save_path = self.clone_dir
        self.log_dir = os.path.join(log_dir, self.github_repo_name)
        os.makedirs(self.log_dir, exist_ok=True)
        print(f'Log directory: {self.log_dir}')
        

        self.auto_build = autobuild

        if not os.path.exists(self.compiled_dir):
            print(f"{self.compiled_dir} does not exist. Thus The repository has not been cloned yet. Cloning the repository within the docker...")
            self._clone_repo()
        else:
            print(f"Repository {self.github_repo_name} is already cloned at {self.compiled_dir}")
        
        # ### After cloning the repo to host directory, change the clone_dir to the docker directory
        # if self.if_docker_executor:
        #     self.docker_repo_path = os.path.join(docker_repo_path, self.github_repo_name)
        #     print("Using docker executor and thus the path to the cloned repo will be: ", self.docker_repo_path)  
        print(f"Using {cores} cores for compilation")
        if self.agents_number == 3:
            heuristic_refine = False # Since we have retriever agent, we do not need heuristic refine
            print("Retrieval method: retriever agent")
        elif self.agents_number == 2 and self.retrieval == False:
            heuristic_refine = False # For ablation study. 
            print("Retrieval method: None")
        elif self.agents_number == 2 and self.perfect_retrieval == True and self.retrieval == True:
            heuristic_refine = False # For ablation study. 
            print("Retrieval method: perfect retrieval; heuristic retrieval is turned off")
        elif self.agents_number == 2 and self.RAG_retrieval == True and self.retrieval == True:
            heuristic_refine = False # For ablation study. 
            print("Retrieval method: RAG retrieval; heuristic retrieval is turned off")
        else:
            heuristic_refine = True
            print("Retrieval method: heuristic")
        prompts = Prompts(
            github_repo=self.github_repo,
            clone_dir=self.compiled_dir,
            # compiled_save_path=self.compiled_save_path,
            sudo_password=self.sudo_password,
            cores=cores,
            refine_times=refine_times,
            model_name = self.model_name,
            heuristic_refine=heuristic_refine,
            perfect_retrieval=self.perfect_retrieval,
            RAG_retrieval=self.RAG_retrieval,
        )
        # print("Compilation agent's system prompt is as follows: ", prompts.compilation_bash_command_generator_system_message())
        agents_created, _ = self._initiate_agents_and_context_handling(prompts)

        chat_summary_path = os.path.join(self.log_dir, 'chat_summary.jsonl')
        print(f'Chat summary path: {chat_summary_path}')
        if os.path.exists(chat_summary_path):
            os.remove(chat_summary_path)
        
        try:
            print("Starting the conversation...")
            logging_session_id = runtime_logging.start(
                logger_type = 'file',
                config = {'filename': 'chat_summary.jsonl'},
            )
            print("Logging session ID: " + str(logging_session_id))
            if self.agents_number == 3:
                print("Max turns: ", self.max_turns)
                
                ### The following code would cause the manager to call bash command generator first every time. Needed to be fixed. Also, it is just not stable. 
                
                # allowed_transitions = {
                #     self.retreiver_agent: [self.execution_agent, self.compilation_agent],
                #     self.execution_agent: [self.retreiver_agent, self.compilation_agent,],
                #     self.compilation_agent: [self.execution_agent,],
                # }
                
                # groupchat = GroupChat(
                #     agents=agents_created, 
                #     messages=[], 
                #     max_round=self.max_turns, 
                #     allowed_or_disallowed_speaker_transitions=allowed_transitions,
                #     speaker_transitions_type="allowed",
                #     send_introductions=True,
                #     )
                
                # self.manager_agent = GroupChatManager(
                #     groupchat=groupchat, 
                #     system_message= prompts.manager_system_message, 
                #     llm_config=self.llm_config,  
                #     is_termination_msg = lambda msg: 'terminate' in msg['content'].lower() or 'happy' in msg['content'].lower,
                #     )
                
                # chat_history_1 = self.retreiver_agent.initiate_chat(
                #     self.manager_agent, message=prompts.initial_message_manager(), silent=False, clear_history=False, summary_args={'summary_method': 'reflection_with_llm', 'summary_prompt': 'Summarize the takeaway from the conversation. Make sure to add ALL the bash commands.'},
                # )
                
                # Heuristic based transition
                ### Built once: it walks the repository directory, and the span
                ### wants the very message the agent was given.
                initial_message_retriever = prompts.initial_message_retriever()
                with tracing.step('retrieval-chat', input=initial_message_retriever,
                                  metadata={'max_turns': 15, 'agents': ['Execution', 'Build_Instructions_Retriever']}) as chat_span:
                    chat_history_1 = self.execution_agent_no_termination.initiate_chat(
                        self.retreiver_agent, max_turns=15,
                        message=initial_message_retriever, silent=False, clear_history=False, summary_args={'summary_method': 'reflection_with_llm', 'summary_prompt': 'Summarize the takeaway from the conversation. Make sure to add ALL the bash commands.'},
                    )
                    tracing.update(chat_span, output=chat_history_1.summary,
                                   metadata={'cost': chat_history_1.cost})
                
                # retriever_last_msg = self.retreiver_agent.last_message(self.execution_agent)
                # if retriever_last_msg != None:
                #     retriever_last_msg_content = retriever_last_msg['content']
                # else:
                #     retriever_last_msg_content = ""
                
                initial_message_compilation = prompts.initial_message_compilation()+"'Build instructions may have been preprocessed and saved in /app/retrieved_instructions.txt. Make sure to check the file for more details.'"
                
                with tracing.step('compilation-chat', input=initial_message_compilation,
                                  metadata={'max_turns': self.max_turns, 'agents': ['Execution', 'Bash_Command_Generator']}) as chat_span:
                    chat_history_2 = self.execution_agent.initiate_chat(self.compilation_agent, max_turns=self.max_turns,
                        message=initial_message_compilation, silent=False, clear_history=False, summary_args={'summary_method': 'reflection_with_llm', 'summary_prompt': 'Summarize the takeaway from the conversation. Make sure to add ALL the bash commands.'},
                        )
                    tracing.update(chat_span, output=chat_history_2.summary,
                                   metadata={'cost': chat_history_2.cost})

            elif len(agents_created) == 2 and self.agents_number == 2:
                initial_message_compilation = prompts.initial_message_compilation()
                with tracing.step('compilation-chat', input=initial_message_compilation,
                                  metadata={'max_turns': self.max_turns, 'agents': ['Execution', 'Bash_Command_Generator']}) as chat_span:
                    chat_history_1 = self.execution_agent.initiate_chat(
                        self.compilation_agent, max_turns=self.max_turns,
                        message=initial_message_compilation, silent=False, clear_history=False, summary_args={'summary_method': 'reflection_with_llm', 'summary_prompt': 'Summarize the takeaway from the conversation. Make sure to add ALL the bash commands.'},
                    )
                    tracing.update(chat_span, output=chat_history_1.summary,
                                   metadata={'cost': chat_history_1.cost})
            else:
                raise ValueError("Agents not created properly. Check the number of agents created. Currently, the number is ", len(agents_created))
            
            ### This will raise error: 'list' object has no attribute '_raise_exception_on_async_reply_functions'
            # chat_history = self.compilation_agent.initiate_chat(
            #     [
            #         {
            #             "recipient": self.installation_agent,
            #             "message": prompts.initial_message_installation(),
            #             "silent": False,
            #             "clear_history": True,
            #             'summary_args':
            #                 {
            #                 "summary_method": "reflection_with_llm",
            #                 # 'summary_prompt': (
            #                 #     'Summarize the takeaway from the conversation. '
            #                 #     'Make sure to add ALL the bash commands.')
            #                 }
            #         },
            #         {
            #             "recipient": self.compilation_agent,
            #             "message": prompts.initial_message_compilation(),
            #             # "silent": False,
            #             'summary_args':
            #                 {
            #                 "summary_method": "reflection_with_llm",
            #                 # 'summary_prompt': (
            #                 #     'Summarize the takeaway from the conversation. '
            #                 #     'Make sure to add ALL the bash commands.')
            #                 }
            #         },
            #     ]
            # )
            
            if print_cost:
                print('-'*10, 'Cost Details','-'*20)
                print(chat_history_1.cost)
                # print(chat_history_2.cost)
            
        except Exception as e:
            print('-'*50)
            print('Following error encountered during the conversation:')
            print(e)
            print('-'*50)
        finally:
            runtime_logging.stop()

            # is_compiled = self.tools.check_for_compilation(self.compiled_save_path)
            # if not self.silent:
            #     print(f'Compilation status: {is_compiled}')
            
            self.temp_dir.cleanup()

            directories = ['__pycache__', '.cache', 'venv']
            for directory in directories:
                if os.path.exists(directory):
                    shutil.rmtree(directory)
                    
        return self.github_repo_name


