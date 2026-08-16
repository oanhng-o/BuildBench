import sys
import os
import json
from tqdm import tqdm
from pathlib import Path
from time import time
import datetime
import configparser

from tempfile import TemporaryDirectory
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor, as_completed
# from elftools.dwarf.descriptions import describe_attr_value
from pyjoern import parse_source
from validation_helper_functions import Dwarf, BinaryInfo
from tools import setup_logger, safe_log
EXPERIMENT_TIME = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def extract_dirs_from_gitsubmodule(git_submodule_file_path):
    """
    Parse .gitmodules and return absolute, normalized paths for each submodule.
    If a section lacks a 'path' key, fall back to the name inside the quotes.
    """
    base_dir = os.path.dirname(git_submodule_file_path)
    cfg = configparser.ConfigParser()
    cfg.read(git_submodule_file_path)

    paths = []
    for section in cfg.sections():
        # Try the explicit 'path' field
        if 'path' in cfg[section]:
            rel_path = cfg[section]['path']
        else:
            # Fallback: section names look like 'submodule "some/path"' 
            try:
                rel_path = section.split('"')[1]
            except Exception:
                # Malformed section header—skip it
                continue

        full_path = os.path.normpath(os.path.join(base_dir, rel_path))
        paths.append(full_path)

    return paths

def find_c_cpp_files(directory, logger,  exclude_dirs=['Test','test', 'Build', 'build'], include_headers=False, max_workers=4):
    """
    Find all .c, .cpp (and optionally .h, .hpp) files in a given directory, 
    excluding specified directories and using multi-threading.

    Parameters:
        directory (str): The absolute path of root directory to search.
        exclude_dirs (list): List of directory names to exclude from search.
        include_headers (bool): Whether to include .h and .hpp files as well.
        max_workers (int): The maximum number of threads to use for searching.

    Returns:
        list: List of all found C/C++ file paths.
    """
    
    max_workers = max_workers * 2 # (I/O‐bound) 
    
    if exclude_dirs is None:
        exclude_dirs = []

    c_cpp_files = []
    file_extensions = ['.c', '.cpp', '.cc', '.cxx', '.c++', '.C', '.cu']

    if include_headers:
        file_extensions += ['.h', '.hpp']

    def process_directory(root_dir):
        
        if not os.path.isabs(root_dir):
            root_dir = os.path.abspath(root_dir)
            
        gitmodules_file = os.path.join(root_dir, '.gitmodules')
        if os.path.isfile(gitmodules_file):
            git_modules_paths = extract_dirs_from_gitsubmodule(gitmodules_file)
        else:
            git_modules_paths = []                
        
        local_files = []
        for root, dirs, files in os.walk(root_dir):
            # Skip excluded directories
            dirs[:] = [
                d for d in dirs
                if d not in exclude_dirs
                and os.path.normpath(os.path.join(root, d)) not in git_modules_paths
            ]            
            
            # Collect relevant files
            for file in files:
                if any(file.endswith(ext) for ext in file_extensions):
                    local_files.append(os.path.join(root, file))
        return local_files

    # Use ThreadPoolExecutor to parallelize the file search
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_directory, directory)]
        for future in futures:
            c_cpp_files.extend(future.result())

    safe_log(logger, 'info', f"Total C/C++ files found: {str(len(c_cpp_files))}")
    return c_cpp_files


def find_linux_compiled_artifacts(directory, logger=None, exclude_dirs =[ 'test', 'Test'], max_workers=4):
    """
    Find all Linux compiled artifacts (e.g., object files, executables, shared libraries) 
    in a given directory, excluding specified directories and using multi-threading.

    Parameters:
        directory (str): The absolute path of root directory to search.
        exclude_dirs (list): List of directory names to exclude from search.
        max_workers (int): The maximum number of threads to use for searching.

    Returns:
        list: List of all found compiled artifact paths.
    """
    
    max_workers = max_workers * 2 # (I/O‐bound)
    
    if exclude_dirs is None:
        exclude_dirs = []

    # Only Linux-related compiled artifact extensions
    artifact_extensions = ['.o', '.a', '.so', '.out', '.bin'] 
    compiled_files = []
    dwarf = Dwarf()
    all_elf_counter = 0
    all_dwarf_info_counter = 0
    def process_directory(root_dir, dwarf: Dwarf):
        # Ensure root_dir is absolute
        if not os.path.isabs(root_dir):
            root_dir = os.path.abspath(root_dir)
            
        elf_counter = 0
        dwarf_info_counter = 0
    
        local_files = []
        
        ### Remove git submodule directories from binary artifacts extractions
        gitmodules_file = os.path.join(root_dir, '.gitmodules')
        if os.path.isfile(gitmodules_file):
            git_modules_paths = extract_dirs_from_gitsubmodule(gitmodules_file)
        else:
            git_modules_paths = []
        
        for root, dirs, files in os.walk(root_dir):
            # Exclude both named directories and submodule paths
            dirs[:] = [
                d for d in dirs
                if d not in exclude_dirs
                and os.path.normpath(os.path.join(root, d)) not in git_modules_paths
            ]
            
            # Collect relevant compiled files
            for file in files:
                file_path = os.path.abspath(os.path.join(root, file))
                try:
                    elffile, dwarf_info = dwarf.is_elf_has_dwarf(file_path)
                    if elffile:
                        elf_counter += 1
                        local_files.append(file_path)
                    if dwarf_info:
                        dwarf_info_counter += 1                    
                except Exception as e:
                    safe_log(logger, 'warning', f"Error checking file {file_path}: {e}")
                    continue
        # return local_files, elf_counter, dwarf_info_counter
        return local_files, elf_counter, dwarf_info_counter


    # Use ThreadPoolExecutor to parallelize the file search
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_directory, directory, dwarf)]
        for future in futures:
            local_files, elf_counter, dwarf_info_counter = future.result()
            
            compiled_files.extend(local_files)
            all_elf_counter += elf_counter
            all_dwarf_info_counter += dwarf_info_counter
    
    safe_log(logger, 'info', (f"Total ELF files found: {str(all_elf_counter)}" ))
    safe_log(logger, 'info',f"Total ELF files with DWARF info found: {str(all_dwarf_info_counter)}")
    return compiled_files




def extract_source_code_functions(repo_directory,  logger,output_file_path=None, max_workers=8) -> tuple:
    """
    Extracts function information from C/C++ source files in a given repository directory and saves it to a JSON file.
    This function traverses through the specified repository directory, identifies all files with .c or .cpp extensions,
    and extracts function information such as function name, start line, end line, and file path. The extracted information
    is saved to the specified output file in JSON format. If the output file already exists, the function appends the new
    information to the existing data.
    Args:
        repo_directory (str): The path to the repository directory to be scanned for C/C++ source files.
        output_file_path (str): The path to the output JSON file where the extracted function information will be saved.
    Returns:
        list: A list of function names extracted from the source files.
    """

    
    source_files = find_c_cpp_files(repo_directory, logger=logger,  max_workers=max_workers) # Traverse through the repository directory and find all the files with .c or .cpp extension, output absolute paths
    assert source_files, f"No C/C++ source files found in directory: {repo_directory}"
    # safe_log(logger, 'info', f"Source files found: {source_files}")
    function_info = []  
    # if os.path.exists(output_file_path):
    #     function_info = json.load(open(output_file_path))
    # else:
    #     function_info = []
    safe_log(logger, 'info', "*"*25 )
    safe_log(logger, 'info',"Extracting function information from source files...")

    with TemporaryDirectory() as temp_dir:
        
        for file in source_files:
            try:
                file_name = os.path.basename(file)
                temp_file_path = os.path.join(temp_dir, file_name)
                os.system(f"cp {file} {temp_file_path}")
            except Exception as e:
                safe_log(logger, 'error', f"Error extracting functions from temporary source code files: {e}")
                continue      
        safe_log(logger, 'info', "Temp directory created successfully, and the C/C++ files are copied to the temp directory.")
        safe_log(logger, 'info', "Extracting function information from temporary source code files...")

        try:              
            funcs = parse_source(temp_dir)
            for func, info in funcs.items():
                if info.macro_count:
                    continue
                function_info.append({
                    "name": info.name,
                    "start_line": info.start_line,
                    "end_line": info.end_line,
                    "file_path": str(info.filename)
                })
        except Exception as e:
            safe_log(logger, 'error', f"Error extracting functions from temporary source code files: {e}")

    if output_file_path:
        with open(output_file_path, 'w') as f:
            f.write(json.dumps(function_info, indent=4))

    source_function_names = [func['name'] for func in function_info]
    source_file_num = len(source_files)
    return source_function_names, source_file_num
        

def process_binary(file_path):
    binary_filename = os.path.basename(file_path)
    binary_name = binary_filename.split('.')[0]
    binary = BinaryInfo(binary_name, file_path)
    functions = binary.function_line_numbers()
    # print("binary_filename: ", binary_filename )
    ### Example output
    '''
    {'emit_ancillary_info': {'filename': 'src/system.h', 'line': 657},
    'idle_string': {'filename': 'src/who.c', 'line': 188},
    'is_tty_writable': {'filename': 'src/who.c', 'line': 317},
    'list_entries_who': {'filename': 'src/who.c', 'line': 537},
    'main': {'filename': 'src/who.c', 'line': 688}}       
    '''
    return functions, file_path

def extract_binary_functions(artifacts_directory,  logger,output_file_path =None, max_workers=8):
    
    max_workers = max_workers - 1 # (CPU‐bound)
    
    artifacts_file_paths = find_linux_compiled_artifacts(directory=artifacts_directory, logger=logger, max_workers=max_workers) # Traverse through the repository directory and find all the files with '.o', '.a', '.so', '.out', '.bin' extension, output absolute paths

    ### A repo that failed to build legitimately has no artifacts. Report it as an
    ### empty result so the caller can record 0% compiled, rather than raising and
    ### taking down the whole run.
    if not artifacts_file_paths:
        safe_log(logger, 'warning', f"No Linux compiled artifacts found in directory: {artifacts_directory}")
        return [], 0

    function_info = []
    function_names = []
    
    safe_log(logger, 'info', "Extracting function information from binary files...")
    #import ipdb; ipdb.set_trace()
    #functions = process_binary(artifacts_file_paths[0])
    #print("functions:"  , functions)
     
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_binary, file_path): file_path for file_path in artifacts_file_paths}
        for future in tqdm(as_completed(futures), total=len(futures)):
            file_path = futures[future]
            try:
                functions, binary_file_path = future.result()
                binary_function_info = {binary_file_path: functions}
                function_info.append(binary_function_info)
                function_names.extend(functions.keys())
            except Exception as e:
                safe_log(logger, 'error',f"Error processing binary {file_path}: {e}")
    if output_file_path:
        with open(output_file_path, 'w') as f:
            f.write(json.dumps(function_info, indent=4))

    binary_artifact_file_num = len(artifacts_file_paths)
    return function_names, binary_artifact_file_num



def validation_pipeline(repo_name, output_file_path, source_directory = None, artifacts_directory=None, threshold=0.5, max_workers=8, date_time = None, logger = None) :
    '''
    pipeline for validation, can work on both source code and binary code seperately or together

    Parameters:
        repo_name (str): The name of the repository
        output_file_path (str): The path to the output file
        source_directory (str): The path to the source code directory
        artifacts_directory (str): The path to the artifacts directory
        threshold (float): The threshold for the validation
        max_workers (int): The maximum number of threads to use for searching
        date_time (str): The date time for the output files
        logger (Logger): The logger object
    '''
    
    start_time = time()
    

        
    if date_time == None:
        print('No date time provided for validation output files, use current time')
        date_time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if logger == None:
        logger = setup_logger(repo_name=repo_name, log_dir=output_file_path, experiment_time=date_time)  
          
    artifacts_file_name = os.path.join(output_file_path, f'{repo_name}_binary_functions_{date_time}.json')
    source_function_result_file_name = os.path.join(output_file_path, f'{repo_name}_source_functions.json')
    
    source_function_names = set()
    binary_function_names = set()
    binary_file_num, source_file_num = None, None
    ### get a list of files in the root directory of source_directory
    if source_directory != None and any(Path(source_directory).iterdir()): 
        
        ### if the source code directory has been validated before, load the source function names from the file
        if os.path.exists(source_function_result_file_name):
            with open(source_function_result_file_name, 'r') as f: 
                function_info = json.load(f)
        else:
            function_info = None   
                 
        if function_info and len(function_info) > 0:
                source_function_names = set([func['name'] for func in function_info])
                logger.info(f"Source functions loaded from pre_existing file {source_function_result_file_name}")
                source_files = find_c_cpp_files(source_directory, logger=logger, max_workers=max_workers)
                source_file_num = len(source_files)
        else:      
            source_function_names, source_file_num = extract_source_code_functions(
                repo_directory=source_directory, 
                logger=logger,
                output_file_path=source_function_result_file_name, 
                max_workers=max_workers)
            source_function_names = set([function.lower() for function in source_function_names]) 
            
        logger.info(f"Source functions: {len(source_function_names)}")
        logger.info("Source extraction is finished")
        
    if artifacts_directory != None and any(Path(artifacts_directory).iterdir()):
        logger.info("*"*25)
        logger.info("Extracting binary functions")
        binary_function_names, binary_file_num = extract_binary_functions(
            artifacts_directory=artifacts_directory, 
            logger=logger, 
            output_file_path=artifacts_file_name, max_workers=max_workers)
        binary_function_names = set([function.lower() for function in binary_function_names]) 
        logger.info(f"Binary functions: {len(binary_function_names)}")
        logger.info('Binary extraction is finished')
    
    logger.info(f"Time taken for validation: {str(time()-start_time)}" ) 
    logger.info(f"source directory: {source_directory}")
    logger.info(f"artifacts directory: {artifacts_directory}")
    if source_directory != None and artifacts_directory != None:
        # Compare the source and binary function names

        missing_functions = source_function_names - binary_function_names

        if len(source_function_names) == 0:
            logger.info("No source functions found")
            return False, None, len(binary_function_names), 0, binary_file_num, source_file_num
        elif len(binary_function_names) == 0:
            logger.info("No binary functions found")
            return False, None, 0, len(source_function_names), binary_file_num, source_file_num

        compiled_percentage = 1 - len(missing_functions) / len(source_function_names)
        compiled_percentage = round(compiled_percentage, 3)
        if compiled_percentage >= threshold:
            logger.info(f"Compiled functions percentage: {compiled_percentage}; the compilation can be considered finished")
            final_result = True
        else:
            logger.info(f"Compiled functions percentage: {compiled_percentage}; the compilation can be considered failed")
            final_result = False
            
        return final_result, compiled_percentage, len(binary_function_names), len(source_function_names), binary_file_num, source_file_num

    ### Reached when either directory is None or empty: nothing could be compared.
    ### Callers unpack six values, so keep the arity stable instead of returning None.
    logger.info("Validation skipped: source and/or artifacts directory is missing or empty")
    return False, None, len(binary_function_names), len(source_function_names), binary_file_num, source_file_num


# def validation_pipeline_by_agent(repo_name: str, max_workers:int =8,):
#     '''
#     pipeline for validation for agent to use, can work on both source code and binary code seperately or together

#     Parameters:
#         source_directory (str): The path to the source code directory
#         artifacts_directory (str): The path to the artifacts directory
#         threshold (float): The threshold for the validation
#         max_workers (int): The maximum number of threads to use for searching

#     '''
#     source_directory = f'/app/cloned_repos/{repo_name}'
#     artifacts_directory = f'/app/compiled_repos/{repo_name}'
#     ### get a list of files in the root directory of source_directory
#     if source_directory != None and any(Path(source_directory).iterdir()): 
        
#         print("start extracting source functions")
#         source_function_names = extract_source_code_functions(
#             repo_directory=source_directory, 
#             logger=None,
#             output_file_path=None, 
#             max_workers=max_workers)

#         if not source_function_names:
#             raise ValueError("source functions are not founds")    
        
#         # for function in source_function_names:
#         #     try:
#         #         function.lower()
#         #     except Exception as e:
#         #         print(f"Error processing function {function}: {e}")
                
    
#         source_function_names = set([f.lower() for f in source_function_names if f is not None])


            
#         print(f"Source functions: {len(source_function_names)}")
#         print("Source extraction is finished")
        
#     if artifacts_directory != None and any(Path(artifacts_directory).iterdir()):
#         print("*"*25)
#         print("Extracting binary functions")
#         binary_function_names = extract_binary_functions(
#             artifacts_directory=artifacts_directory, 
#             logger=None, 
#             output_file_path=None,
#             max_workers=max_workers)
#         if not binary_function_names:
#             raise ValueError("binary functions are not founds")    
        
#         # for function in binary_function_names:
#         #     try:
#         #         function.lower()
#         #     except Exception as e:
#         #         print(f"Error processing function {function}: {e}")
                
#         binary_function_names = set([f.lower() for f in binary_function_names if f is not None])      
        
#         print(f"Binary functions: {len(binary_function_names)}")
#         print('Binary extraction is finished')
        
#     if source_directory != None and artifacts_directory != None:
#         # Compare the source and binary function names
        
#         missing_functions = source_function_names - binary_function_names

#         if len(source_function_names) == 0:
#             print("No source functions found")
#             return False, None
#         elif len(binary_function_names) == 0:
#             print("No binary functions found")
#             return False, None
        
#         compiled_percentage = 1 - len(missing_functions) / len(source_function_names)
#         compiled_percentage = round(compiled_percentage, 3)
#         print(f"Compiled functions percentage: {compiled_percentage}")
#         return compiled_percentage

if __name__ == "__main__":

    start = time()
    max_workers = 10
    # Example usage:
    # validation_pipeline(repo_name="example_repo",
    #                     source_directory="cloned_repos/example_repo",
    #                     artifacts_directory="compiled_repos/example_repo",
    #                     output_file_path="compiled_results/output_files",
    #                     max_workers=max_workers)
    print("Time taken: ", time()-start)