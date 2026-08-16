import os
import sys
import argparse
import pandas as pd
from tqdm import tqdm
import json
import docker
from time import time
import datetime
import filecmp
from concurrent.futures import ThreadPoolExecutor, as_completed
import langfuse_tracing as tracing
from default_values import DEFAULT_VALUES
from log_parse import generate_parsed_files
from validation_pipeline import validation_pipeline
from tools import setup_logger, remove_and_copy_directory_wrapper, clone_repository, parse_args, get_target_github_repos


CLONED_REPOS_FOLDER_DIR = 'cloned_repos'
COMPILED_REPOS_FOLDER_DIR = 'compiled_repos'
COMPILED_RESULTS_FOLDER_DIR = 'compiled_results'
SRC_FOLDER_DIR = 'src'
AUTOGEN_LOGS_FOLDER_DIR = 'autogen_logs'
ALL_LOGS_FOLDER_DIR = 'all_logs'
EXPERIMENT_START_TIME = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
DATA_PATH = 'data/sampled_repos_385_cleaned_higher_split.jsonl'



def compile_in_container(github_repo, repo_name, cloned_repos_dir, container, autogen_logs_dir, logger):
    try:
        remove_and_copy_directory_wrapper(container= container, repo_name = repo_name, logger = logger, cloned_repos_path=f"{CLONED_REPOS_FOLDER_DIR}", compiled_repos_path=f"{COMPILED_REPOS_FOLDER_DIR}")

        # Execute compilation.py inside container
        # # make it async
        # _, stream = container.exec_run(
        #     cmd=f"find /usr/local /usr /opt -type f > /app/before_install.txt",
        #     workdir="/app/",
        #     stream=True  # Enable streaming
        # )
        _, stream = container.exec_run(
            cmd=f"/opt/venv/bin/python /app/src/compilation.py --repo_url {github_repo} --compiled_dir=/app/{COMPILED_REPOS_FOLDER_DIR}",
            workdir="/app/",
            stream=True  # Enable streaming
        )
        for data in stream:
            logger.info(data.decode('utf-8'))

        # Copy autogen_logs from container to host
        # autogen_logs_host_path = os.path.join(autogen_logs_dir, repo_name)
        # os.makedirs(autogen_logs_host_path, exist_ok=True)
        # copy_directory_from_container(container, f'/app/autogen_logs', autogen_logs_dir)

        # Parse the logs
        # generate_parsed_files(autogen_logs_dir)
        
        return True  # Indicate success
    except Exception as e:
        logger.error(f"Compilation failed for {repo_name}: {e}")
        return False  # Indicate failure


def main(args):
    host_project_dir = args.host_project_dir
    src_dir = os.path.join(host_project_dir, SRC_FOLDER_DIR)
    ### copy this folder to another folder
    
    cloned_dir = os.path.join(host_project_dir, CLONED_REPOS_FOLDER_DIR)
    compiled_repos_dir = os.path.join(host_project_dir, COMPILED_REPOS_FOLDER_DIR)
    autogen_logs_dir = os.path.join(host_project_dir, AUTOGEN_LOGS_FOLDER_DIR)
    logs_dir = os.path.join(host_project_dir, ALL_LOGS_FOLDER_DIR)
    bash_commands_generated_dir = os.path.join(host_project_dir, 'bash_commands_generated')
    compiled_results_dir = os.path.join(host_project_dir, COMPILED_RESULTS_FOLDER_DIR)
    
    results_file_path = os.path.join(compiled_results_dir, 'results.json')
    os.makedirs(compiled_results_dir, exist_ok=True)
    if not os.path.exists(results_file_path):
        with open(results_file_path, 'w') as f:
            json.dump({}, f)
        
    args_dict = vars(args)
    # Embed the args_dict into the environment variables to ensure they can be assessed thereafter by the scripts within the docker container. Whereas for the rest of the default values that are hardcoded, can be assessible directly from the default_values.py within the docker container.
    docker_env_vars = {}
    for key, value in args_dict.items():
        if type(value) != str:
            str_value = str(value)
        else:
            str_value = value
        docker_env_vars[key.upper()] = str_value

    # This process scores the traces the containers write, so it has to agree
    # with them on the run id -- hence one shared value, resolved here and
    # passed down. EXPERIMENT_START_TIME names the run in the logs; reuse it.
    os.environ.setdefault("BUILDBENCH_RUN_ID", EXPERIMENT_START_TIME)
    tracing.init()
    docker_env_vars.update(tracing.forwarded_env())

    ### Get the list of target GitHub repositories based on args
    github_repos = get_target_github_repos(args, DEFAULT_VALUES, data_path=args.data_path, github_token=args.github_token)
    

    client = docker.from_env() # Connect to the docker daemon
    
    
    def handle_repo(github_repo):
        repo_start_time = time()
        repo_name = github_repo.split('/')[-1].replace('.git', '')
        
        compiled_repo_dir = os.path.join(compiled_repos_dir, repo_name) # Create a directory for the compiled files for the repo
        os.makedirs(compiled_repo_dir, exist_ok=True)
        repo_logs_dir = os.path.join(logs_dir, repo_name)
        os.makedirs(repo_logs_dir, exist_ok=True)        
        repo_output_dir = os.path.join(compiled_results_dir, repo_name)
        os.makedirs(repo_output_dir, exist_ok=True)
    
        logger = setup_logger(repo_name, repo_logs_dir, EXPERIMENT_START_TIME)
        logger.info(f"Starting compilation process for {github_repo}...")
        cloned_repo_dir = clone_repository(repo_url= github_repo, save_path=cloned_dir, logger=logger) # Check if the repo is already cloned; if not, clone it
        # Start the Docker container
        container = client.containers.run(
            image=args.docker_image,
            command="tail -f /dev/null",
            detach=True,
            working_dir="/app",
            volumes={
                src_dir: {"bind": f"/app/{SRC_FOLDER_DIR}", "mode": "ro"},
                cloned_dir: {"bind": f"/app/{CLONED_REPOS_FOLDER_DIR}", "mode": "ro"},
                compiled_repos_dir: {"bind": f"/app/{COMPILED_REPOS_FOLDER_DIR}", "mode": "rw"},
                # autogen_logs_dir: {"bind": f"/app/{AUTOGEN_LOGS_FOLDER_DIR}", "mode": "rw"},
            },
            environment=docker_env_vars,
        )

        try:
            # Copy src_dir to container
            # copy_directory_to_container(container, src_dir, '/app')
            # logger.info("Copied src folder to container.")

            # Run the compilation process in the container
            success = compile_in_container(github_repo, repo_name, cloned_dir, container, autogen_logs_dir, logger)

            if success:
                logger.info(f'Starting validation for {repo_name} with the source_directory being {cloned_repo_dir}, and artifacts_directory being {compiled_repo_dir}. Output files will be saved to {repo_output_dir}...')
                
                is_compiled, compiled_percentage, len_binary_func, len_source_func, binary_file_num, source_file_num  = validation_pipeline(repo_name=repo_name, output_file_path=repo_output_dir, source_directory=cloned_repo_dir, artifacts_directory=compiled_repo_dir, threshold=0.5, max_workers=8, date_time=EXPERIMENT_START_TIME, logger = logger)
                logger.info(f'Validation process completed for {args.github_repo}.')
                
                elapsed_time = time() - repo_start_time
                ### Save the results to a file
                repo_result = {
                    # "is_compiled": is_compiled,
                    "compiled_percentage": compiled_percentage,
                    "repo_experiment_start_time": EXPERIMENT_START_TIME,
                    "repo_execution_time": f"{elapsed_time:.2f} seconds",
                    "len_binary_func": len_binary_func,
                    "len_source_func": len_source_func,
                    'binary_file_num': binary_file_num,
                    'source_file_num': source_file_num
                }
                
                with open(results_file_path, 'r') as f:
                    previous_results = json.load(f)

                if repo_name not in previous_results:
                    previous_results[repo_name] = []
                
                previous_results[repo_name].append(repo_result)
                
                with open(results_file_path, 'w') as f:
                    json.dump(previous_results, f, indent=4)

                ### Same trace the container's agents wrote: the id is derived
                ### from the shared run id and the repo name.
                tracing.score(repo_name, "compiled_percentage", float(compiled_percentage))
                tracing.score(repo_name, "is_compiled", float(bool(is_compiled)), data_type="BOOLEAN")

            return repo_name, success
        
        
        except docker.errors.DockerException as e:
            logger.error(f"Docker error occurred for {repo_name}: {e}")
            return repo_name, False
        except Exception as e:
            logger.error(f"An error occurred for {repo_name}: {e}")
            return repo_name, False
        finally:
            # Stop and remove the container
            container.stop()
            # container.remove()
            

    # Use ThreadPoolExecutor for multithreading
    max_workers = min(8, 4*len(github_repos))  # Adjust number of workers as needed
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(handle_repo, repo) for repo in github_repos]

        with tqdm(total=len(github_repos)) as pbar:
            for future in as_completed(futures):
                repo_name, success = future.result()
                status = "SUCCESS" if success else "FAILURE"
                print(f"The compilation process of {repo_name} is {status} in the Docker. Logs are saved in {logs_dir}/{repo_name}.log")
                pbar.update(1)
                
                

if __name__ == '__main__':
    start = time()
    args = parse_args(default_values=DEFAULT_VALUES)
    args_save_location = os.path.join(args.host_project_dir, 'src/args.json')
    with open(args_save_location, 'w') as f:
        json.dump(vars(args), f)
        
    try:
        main(args)
    finally:
        tracing.shutdown()
    print("Total time taken for the entire process:", time() - start)
