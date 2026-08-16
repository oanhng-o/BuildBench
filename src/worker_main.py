import os
import sys
import fcntl
import pandas as pd
from tqdm import tqdm
import json
from time import time, sleep
import datetime
import shutil
import subprocess
import langfuse_tracing as tracing
from default_values import DEFAULT_VALUES
from validation_pipeline import validation_pipeline
from tools import setup_logger, remove_and_copy_directory_wrapper, create_tarball, extract_tarball_subprocess, clone_repository


def resolve_repo_url():
    """
    Pick the repository to build. Two ways in, so the same worker serves both
    the K8S Indexed Job (ConfigMap + JOB_INDEX) and a plain `docker run`:

      1. REPO_URL  - build exactly this URL, no config file needed.
      2. JOB_INDEX - index into the JSON list at REPOS_CONFIG.
    """
    repo_url = os.environ.get("REPO_URL")
    if repo_url:
        print(f"Starting compilation for repo from REPO_URL: {repo_url}")
        return repo_url

    repos_config = os.environ.get("REPOS_CONFIG", "/app/config/repos.json")
    if not os.path.exists(repos_config):
        print(f"Neither REPO_URL is set nor does {repos_config} exist. Exiting.")
        sys.exit(1)

    with open(repos_config, "r") as f:
        repos = json.load(f)

    job_index = int(os.environ.get("JOB_INDEX", "0"))
    if job_index >= len(repos):
        print(f"JOB_INDEX {job_index} is out of range for {len(repos)} repos in {repos_config}. Exiting.")
        sys.exit(1)

    repo_url = repos[job_index]
    print(f"Starting compilation for repo at index {job_index}: {repo_url}")
    return repo_url


REPO_URL = resolve_repo_url()
if not REPO_URL:
    print("No REPO_URL provided. Exiting.")
    sys.exit(1)

CLONED_DIR = os.environ.get("CLONED_DIR", DEFAULT_VALUES["CLONED_DIR"])
### Shared output directory: an NFS mount on K8S, a bind mount under plain Docker
NFS_COMPILED_DIR = os.environ.get("COMPILED_DIR", DEFAULT_VALUES["COMPILED_DIR"])
### Node-local directory to actually conduct the compilation, kept off the shared
### mount because builds write many small files
BUILD_DIR = os.environ.get("BUILD_DIR", DEFAULT_VALUES["K8S_COMPILED_DIR"])
### How the build output reaches NFS_COMPILED_DIR: 'tarball' (K8S default,
### cheap over NFS), 'dir' (what postprocessing/ expects), or 'both'
OUTPUT_MODE = os.environ.get("OUTPUT_MODE", "tarball").lower()
if OUTPUT_MODE not in ("tarball", "dir", "both"):
    print(f"Invalid OUTPUT_MODE '{OUTPUT_MODE}'. Expected one of: tarball, dir, both. Exiting.")
    sys.exit(1)
if not os.path.exists(BUILD_DIR):
    os.makedirs(BUILD_DIR)
RESULTS_DIR = os.environ.get("RESULTS_DIR", DEFAULT_VALUES["RESULTS_DIR"])
AUTOGEN_LOGS_DIR = DEFAULT_VALUES["AUTOGEN_LOGS_DIR"]
ALL_LOGS_DIR = os.environ.get("ALL_LOGS_DIR", DEFAULT_VALUES["ALL_LOGS_DIR"])
CORES = os.environ.get("CORES", DEFAULT_VALUES["CORES"])

for _directory in (CLONED_DIR, NFS_COMPILED_DIR, RESULTS_DIR, ALL_LOGS_DIR, AUTOGEN_LOGS_DIR):
    os.makedirs(_directory, exist_ok=True)

def transfer_files_to_nfs(repo_name, logger, execution_start_time, build_repo_dir):
    try:
        # Copy the compiled repo to the shared output directory
        tarball_path = f'/app/{repo_name}.tar.gz'
        logger.info(f"Creating tarball for {repo_name} at {tarball_path}...")
        create_tarball(source_dir=build_repo_dir, tarball_path=tarball_path)
        create_tarball_time = time() - execution_start_time
        logger.info(f"Created tarball for {repo_name} using {create_tarball_time} seconds.")
        # First, copy tarball to the output directory as {repo_name}.tar.gz
        tarball_nfs_destination = os.path.join(NFS_COMPILED_DIR, os.path.basename(tarball_path))
        shutil.copy2(tarball_path, tarball_nfs_destination)  # copy2 preserves metadata
        move_to_nfs_time = time() - execution_start_time
        logger.info(f"Moved compiled repo to {NFS_COMPILED_DIR} using {move_to_nfs_time} seconds.")

        # Then, unless we were asked for a bare tarball, extract it to
        # {NFS_COMPILED_DIR}/{repo_name} so postprocessing/ can read it directly
        if OUTPUT_MODE in ("dir", "both"):
            nfs_repo_path = os.path.join(NFS_COMPILED_DIR, repo_name)
            os.makedirs(nfs_repo_path, exist_ok=True)
            extract_tarball_subprocess(tarball_nfs_destination, nfs_repo_path)
            logger.info(f"Extracted tarball to {nfs_repo_path}")
            if OUTPUT_MODE == "dir":
                os.remove(tarball_nfs_destination)
                logger.info(f"Removed tarball: {tarball_nfs_destination}")
            extract_and_remove_time = time() - execution_start_time
            logger.info(f"Extracted tarball using {extract_and_remove_time} seconds.")

    except Exception as e:
        logger.error(f"Error transferring files to {NFS_COMPILED_DIR}: {e}")
        # sys.exit(1)

def append_result(results_file_path, repo_name, repo_result):
    """
    Append one repo's result to the shared results.json under an exclusive lock.

    Several workers write to the same file when repos run in parallel, so the
    read-modify-write has to be atomic or results get silently dropped.
    """
    with open(results_file_path, 'a+') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read().strip()
            previous_results = json.loads(content) if content else {}

            previous_results.setdefault(repo_name, []).append(repo_result)

            f.seek(0)
            f.truncate()
            json.dump(previous_results, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def main():
    repo_name = REPO_URL.split('/')[-1].replace('.git', '')

    ### The build itself is traced by the compilation.py subprocess below; the
    ### worker only joins that trace, to score it once validation has a number.
    ### init() also exports the run id, which the subprocess inherits and uses to
    ### derive the same trace id.
    tracing.init()

    start_time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    execution_start_time = time()
    repo_logs_dir = os.path.join(ALL_LOGS_DIR, repo_name)
    os.makedirs(repo_logs_dir, exist_ok=True)    
    
    ### Setup logger
    logger = setup_logger(repo_name, repo_logs_dir, start_time)
    
    cloned_repo_dir = os.path.join(CLONED_DIR, repo_name)
    build_repo_dir = os.path.join(BUILD_DIR, repo_name)
    results_file_path = os.path.join(RESULTS_DIR, f'results.json')
        
    repo_output_dir = os.path.join(RESULTS_DIR, repo_name)
    os.makedirs(repo_output_dir, exist_ok=True)
    
    # Clone the repo if not present
    clone_repository(repo_url=REPO_URL, save_path=CLONED_DIR, logger=logger)
    
    
        
    clone_time = time() - execution_start_time
    logger.info(f"Cloned {REPO_URL} to {cloned_repo_dir} using {clone_time} seconds.")
            
            

    # Run compilation steps (this replaces the docker exec logic)
    
    remove_and_copy_directory_wrapper(repo_name=repo_name, logger=logger, container=None, cloned_repos_path=CLONED_DIR, compiled_repos_path=BUILD_DIR)

    total_setup_time = time() - execution_start_time
    logger.info(f"Setup completed for {repo_name} using {total_setup_time} seconds.")

    logger.info(f"Compiling {repo_name}...")

    # Mark the compiled repo as a safe directory for git
    safe_dir = build_repo_dir
    try:
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", safe_dir],
            check=True
        )
        logger.info(f"Added {safe_dir} to git safe.directory.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to add {safe_dir} to git safe.directory: {e}")
        
    # Assuming compilation.py is in /app/src and can run directly
    # and that it uses env vars or parameters to know what to compile
    # NOTE: This is specifically for running on K8S with the virtual env
    cmd = ["/opt/venv/bin/python", "/app/src/compilation.py", "--repo_url", REPO_URL, '--compiled_dir', BUILD_DIR]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    for line in proc.stdout:
        logger.info(line.rstrip())

    return_code = proc.wait()
    if return_code != 0:
        errors = proc.stderr.read()
        logger.error(f"Compilation failed:\n{errors}")
        tracing.score(repo_name, "worker_status", "compilation_failed",
                      comment=f"compilation.py exited with {return_code}", data_type="CATEGORICAL")
        tracing.shutdown()
        sys.exit(1)
    logger.info(f"Compilation finished for {repo_name}.")
    
    compile_time = time() - execution_start_time
    logger.info(f"Compiled {repo_name} using {compile_time} seconds.")
    
    # Transfer files to the shared output directory
    transfer_files_to_nfs(repo_name, logger, execution_start_time, build_repo_dir)

    # Validation step
    
    # Install Java since we need it for the validation step and we removed it from the docker image
    # subprocess.run(["apt-get", "update"], check=True)
    # subprocess.run(["apt-get", "install", "-y", "openjdk-17-jdk"], check=True)
    # subprocess.run(["apt-get", "install", "-y", "openjdk-17-jre"], check=True)
    # subprocess.run(["rm", "-rf", "/var/lib/apt/lists/*"], check=True)
    # logger.info(f"Installed Java for {repo_name}.")
    
    # Run validation pipeline
    logger.info(f"Running validation for {repo_name}...")
    is_compiled, compiled_percentage, len_binary_func, len_source_func, binary_file_num, source_file_num = validation_pipeline(repo_name=repo_name, output_file_path=repo_output_dir, source_directory=build_repo_dir, artifacts_directory=build_repo_dir, threshold=0.5, max_workers=int(CORES), date_time=start_time, logger = logger)
    logger.info(f'Validation process completed for {repo_name}.')
    logger.info(f'Compiled percentage: {compiled_percentage}')
    
    validation_time = time() - execution_start_time
    logger.info(f"Validated {repo_name} using {validation_time} seconds.")
    
    # Update results.json
    total_time = time() - execution_start_time
    repo_result = {
        "compiled_percentage": compiled_percentage,
        "clone_time": f"{clone_time:.2f} seconds",
        "compile_time": f"{compile_time:.2f} seconds",
        "validation_time": f"{validation_time:.2f} seconds",
        "total_setup_time": f"{total_setup_time:.2f} seconds",
        "total_execution_time": f"{total_time:.2f} seconds",
        "len_binary_func": len_binary_func,
        "len_source_func": len_source_func,
        'binary_file_num': binary_file_num,
        'source_file_num': source_file_num
    }
    logger.info(f"Final result for {repo_name}: {repo_result}")
    append_result(results_file_path, repo_name, repo_result)

    ### Lands on the trace the agents wrote, so the conversation and the score it
    ### earned sit side by side in Langfuse.
    tracing.score(repo_name, "compiled_percentage", float(compiled_percentage))
    tracing.score(repo_name, "is_compiled", float(bool(is_compiled)), data_type="BOOLEAN")
    tracing.shutdown()

    logger.info(f"Task completed for {repo_name}. Exiting worker.")
    return True

if __name__ == "__main__":
    main()