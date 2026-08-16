# orchestrator/orchestrator.py
import os
import yaml
from kubernetes import client, config
import subprocess
import json
import argparse
import logging
import pandas as pd
import datetime
from src.default_values import DEFAULT_VALUES
from src.tools import load_file, get_target_github_repos, parse_args, read_password, sanitize_k8s_name, setup_logger, get_perfect_retrieval_labels

orchestrator_log_dir = "orchestrator_logs"
yaml_file_dir = "yaml_files"
start_time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
logger = setup_logger("orchestrator", orchestrator_log_dir , start_time)


JOB_TEMPLATE_PATH = "job-template.yaml"

def load_job_template():
    with open(JOB_TEMPLATE_PATH, 'r') as f:
        return yaml.safe_load(f)

# def create_job_manifest(args, repo_url, base_name="llm-compiler"):
#     job_template = load_job_template()
#     job_name = f"{base_name}-{repo_url.split('/')[-1].replace('.git','')}"
#     sanitized_name = sanitize_k8s_name(job_name)
#     job_template['metadata']['name'] = sanitized_name
    
#     # Add repo_url as an environment variable
#     env_vars = [{'name': 'REPO_URL', 'value': repo_url}]
    
#     # Add default values as environment variables
#     for key, value in DEFAULT_VALUES.items():
#         if type(value) != str:
#             str_value = str(value)
#         else:
#             str_value = value
#         env_vars.append({'name': key, 'value': str_value})
    
#     env_vars.append({'name': 'API_KEY', 'value': args.api_key})
#     env_vars.append({'name': 'SUDO_PASSWORD', 'value': args.sudo_password})
    
#     job_template['spec']['template']['spec']['containers'][0]['env'] = env_vars
#     job_template['spec']['template']['spec']['containers'][0]['image'] = args.docker_image
#     if args.image_pull_policy:
#         job_template['spec']['template']['spec']['containers'][0]['imagePullPolicy'] = 'Always'
#     else:
#         job_template['spec']['template']['spec']['containers'][0]['imagePullPolicy'] = 'IfNotPresent'
    
#     return job_template

# def apply_job_manifest(job_manifest):
#     with open("temp-job.yaml", 'w') as f:
#         yaml.dump(job_manifest, f)
#     # Apply the job
#     subprocess.run(['kubectl', 'delete', 'job', job_manifest['metadata']['name']], check=False)
#     subprocess.run(["kubectl", "apply", "-f", "temp-job.yaml"], check=True)
#     os.remove("temp-job.yaml")



def main(args):

    repos = get_target_github_repos(args, default_values=DEFAULT_VALUES, data_path=args.data_path, github_token=args.github_token)
    num_repos = len(repos)
    logger.info(f"Compiling {num_repos} repositories...")
    
    args_save_location = os.path.join(args.host_project_dir, 'src', 'args_k8s.json')
    with open(args_save_location, 'w') as f:
        json.dump(vars(args), f)

    # retrieval_labels = get_perfect_retrieval_labels(args.retrieval_input_path)
    if len(repos) == 1:
        configmap_name = "llm-compiler-repos-test"
    else:
        configmap_name = "llm-compiler-repos"
    configmap_manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name
        },
        "data": {
            # Dump the entire repos array as JSON
            "repos.json": json.dumps(repos),
            # "retrieval_labels.json": json.dumps(retrieval_labels)
        }
    }
    config_map_path =  os.path.join(yaml_file_dir, f'configmap-{start_time}.yaml')
    with open(config_map_path, 'w') as f:
        yaml.dump(configmap_manifest, f)
    
    # Apply the ConfigMap
    subprocess.run(["kubectl", "apply", "-f", config_map_path], check=True)
    logger.info(f"Created/Updated ConfigMap '{configmap_name}' with {num_repos} repos.")

    job_template = load_job_template()

    job_name = f"llm-compiler-job-{args.model_name.lower().replace('.', '-').replace('/', '-').replace(':', '-')}-temp-refine-2"
    if len(job_name) > 63:
        job_name = job_name[:55]
    if len(repos) == 1:
        job_name = 'test'
    job_template["metadata"]["name"] = job_name
    job_template["spec"]["completions"] = num_repos
    job_template["spec"]["parallelism"] = args.k8s_parallelism
    job_template["spec"]["completionMode"] = "Indexed"
    job_template["spec"]["maxFailedIndexes"] = num_repos
    job_template['spec']['template']['spec']['volumes'][1]['configMap']['name'] = configmap_name

    # job_template["spec"]["backoffLimit"] = args.backoff_limit ### TODO: So far the backoff limit is set to 100, should increase it if you are creating more than 50 jobs in total. 

    container = job_template["spec"]["template"]["spec"]["containers"][0]
    
    # Add default values as environment variables
    env_vars = []
    args_dict = vars(args)
    
    # Embed the args_dict into the environment variables to ensure they can be assessed thereafter by the scripts within the docker container. Whereas for the rest of the default values that are hardcoded, can be assessible directly from the default_values.py within the docker container.
    for key, value in args_dict.items():
        if type(value) != str:
            str_value = str(value)
        else:
            str_value = value
        env_vars.append({'name': key.upper(), 'value': str_value})
    
    # env_vars.append({'name': 'API_KEY', 'value': args.api_key})
    # env_vars.append({'name': 'SUDO_PASSWORD', 'value': args.sudo_password})

    # Tracing config is not an argparse argument; take it from the environment
    # the orchestrator itself runs in. Absent, the pods simply do not trace.
    # BUILDBENCH_RUN_ID makes one submission one Langfuse session.
    env_vars.append({'name': 'BUILDBENCH_RUN_ID', 'value': os.environ.get('BUILDBENCH_RUN_ID', start_time)})
    for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL',
                'LANGFUSE_TRACING', 'LANGFUSE_ENVIRONMENT'):
        value = os.environ.get(key)
        if value:
            env_vars.append({'name': key, 'value': value})
    if "env" not in container:
        container["env"] = []
    container["env"].extend(env_vars)
    container['image'] = args.docker_image
    
    # imagePullPolicy is set to 'IfNotPresent' by default to avoid pulling the image every time
    if args.image_pull_policy:
        container['imagePullPolicy'] = 'Always'
    else:
        container['imagePullPolicy'] = 'IfNotPresent'
    
    container['resources']['requests']['cpu'] = args.cores
    
    try:       
        # save the job template to a file
        yaml_file_path =  os.path.join(yaml_file_dir, f'job-{start_time}.yaml')
        with open(yaml_file_path, 'w') as f:
            yaml.dump(job_template, f)  
        
        # Apply the job    
        subprocess.run(["kubectl", "delete", "job", job_name], check=False)
        subprocess.run(["kubectl", "apply", "-f", yaml_file_path], check=True)
        logger.info(f"Submitted job '{job_name}' to Kubernetes.")    
    except Exception as e:
        logger.info(f"Failed to submit job: {e}")
        raise e

if __name__ == "__main__":
    args = parse_args(DEFAULT_VALUES)
    main(args)
