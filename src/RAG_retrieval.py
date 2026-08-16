import os
import json
import re

from pprint import pprint
from urllib.parse import urlparse
from tqdm import tqdm
from langchain_chroma import Chroma
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_community.document_loaders import WebBaseLoader
import tiktoken 
import nest_asyncio
nest_asyncio.apply()
import env_config

from build_info_retrieval import get_readme_path, get_build_dict, read_file
from tools import is_valid_url


def count_tokens(string: str, encoding_name="cl100k_base") -> int:
    """Returns the number of tokens in a text string."""
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

class RAGRetrieval:
    def __init__(self, repo_dir: str, index_path: str, include_urls: bool = False, url_visit_depth: int = 1):
        '''
        Initialize RAGRetrieval with repository directory, index path, and URL settings.
        Args:
            repo_dir (str): Path to the local repository directory.
            index_path (str): Path to store/load the FAISS index.
            include_urls (bool): Whether to include URLs from README for retrieval.
            url_visit_depth (int): Depth of URL visits for web scraping. default is 1. A depth of 1 means only the URLs in the README will be visited. A depth of 2 means the URLs found in the README and the URLs found in those pages will be visited.
        '''
        self.repo_dir = repo_dir
        self.index_path = index_path
        self.include_urls = include_urls
        self.url_visit_depth = url_visit_depth

        self.persist_db_name = "chroma_persist"
        if self.include_urls:
            self.persist_db_name += f"_with_urls_depth_{self.url_visit_depth}"
            
        self.total_document_tokens = 0
        self.total_url_tokens = 0
        
        
        self.index_path = index_path
        self.embeddings = OpenAIEmbeddings(model='text-embedding-3-small', api_key=env_config.get("OPENAI_KEY"))
        self.vector_store = None

    def get_local_documents_path(self):
        '''
        Get local documents for RAG retrieval
        '''
        
        # key documents path for RAG retrieval
        endswith_list = [".markdown", ".md", ".rst"]
        keywords = ["compile","build","compilation", "instructions", "install", "setup", "contributing", "how to", 'readme']        

        compilation_ins_doc = []

        # find possible compilation instruction documents
        for root, dirs, files in os.walk(self.repo_dir):
            for file in files:
                if any(file.lower().endswith(f"{suffix}") for suffix in endswith_list):
                    compilation_ins_doc.append(os.path.join(root,file))
                if file.lower() in keywords:
                    compilation_ins_doc.append(os.path.join(root,file))
        return compilation_ins_doc

    def load_documents(self, doc_path_list: list):
        
        documents = []
        for file_path in doc_path_list:
            try:
                with open(file_path,'r',errors="ignore") as fp:
                    content = fp.read()
                    documents.append(
                        Document(page_content=content, metadata={"source":file_path})
                    )
                    self.total_document_tokens += count_tokens(content)
            except Exception as e:
                print(f"[!] Failed to read {file_path}:\n{e}")
        return documents

    def get_readme_content(self):
        build_tools_dict = get_build_dict(self.repo_dir)

        if len(build_tools_dict) == 0:
            build_tools_dict = None

        readme_full_path = get_readme_path(build_tools_dict, self.repo_dir)
        if readme_full_path is None:
            readme_content = None
            print("Warning: No readme file found, readme content is set to None")
        else:
            readme_content = read_file(readme_full_path) # Get the content of the README file
        
        return readme_content

    def fetch_urls_from_docs(self, documents: list[Document]):
        # Extract URLs from the document content
        urls = set()
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]*'
        for doc in documents:
            content = doc.page_content
            found_urls = re.findall(url_pattern, content)

            # Validate and clean URLs
            for url in found_urls:
                cleaned_url = url.strip().rstrip('.,;:!?)')  # Remove trailing punctuation
                if is_valid_url(cleaned_url):
                    urls.add(cleaned_url)
        
        return list(urls)

    def load_urls(self, urls: list):
        loader = WebBaseLoader(urls)
        #NOTE: Set a reasonable rate limit to avoid overwhelming servers
        loader.requests_per_second = 5
        loader.continue_on_failure = True
        docs = loader.aload()
        return docs

    def load_url_documents(self):
        ### Get initial URLs from README content
        url_documents: list[Document] = []

        # 1) Seed from README
        readme_content = self.get_readme_content()
        if readme_content is None:
            print("Warning: No readme content found, skipping URL extraction")
            return [], []

        readme_document = Document(page_content=readme_content, metadata={"source": "README"})
        initial_urls = self.fetch_urls_from_docs([readme_document])

        # Nothing to do if no URLs in README
        if not initial_urls:
            return [], []
        
        # Config / guards
        visited_urls: set[str] = set()
        visited_urls.update(initial_urls)

        # 2) Load level-1 (README URLs)
        try:
            current_docs = self.load_urls(list(initial_urls))
        except Exception as e:
            print(f"Warning: Failed to load initial URLs: {e}")
            current_docs = []

        url_documents.extend(current_docs)
        self.total_url_tokens += sum(count_tokens(doc.page_content) for doc in current_docs)

        # 3) BFS crawl for deeper levels
        depth = 1
        while depth < getattr(self, "url_visit_depth", 1):
            depth += 1

            # Collect next-frontier URLs from the docs we just loaded
            next_urls = self.fetch_urls_from_docs(current_docs)

            # De-duplicate already visited
            next_urls = [u for u in next_urls if u not in visited_urls]

            if not next_urls:
                break

            # Load next level
            try:
                current_docs = self.load_urls(next_urls)
            except Exception as e:
                print(f"Warning: Failed to load depth {depth} URLs: {e}")
                current_docs = []

            url_documents.extend(current_docs)
            self.total_url_tokens += sum(count_tokens(doc.page_content) for doc in current_docs)
            visited_urls.update(next_urls)
        return url_documents, list(visited_urls)

    def save_metadata(self, compilation_ins_doc_paths, visited_urls):
        metadata_name = self.persist_db_name + '_metadata.json'
        meta_data_persist_path = os.path.join(self.index_path, metadata_name)
        if os.path.exists(meta_data_persist_path):
            with open(meta_data_persist_path, 'r') as f:
                previously_saved_metadata = json.load(f)
        else:
            previously_saved_metadata = {}
            
        repo_metadata = {
            'visited_local_documents': compilation_ins_doc_paths,
            'visited_url_documents': visited_urls,
            'local_documentation_tokens': self.total_document_tokens,
            'url_documentation_tokens': self.total_url_tokens
        }
        previously_saved_metadata[metadata_name] = repo_metadata
        with open(meta_data_persist_path, 'w') as f:
            json.dump(previously_saved_metadata, f, indent=4)
        
    def create_vector_store(self):
        documents = []
        
        vector_store_save_dir = os.path.join(self.index_path, self.persist_db_name)
        os.makedirs(vector_store_save_dir, exist_ok=True)

        if os.listdir(vector_store_save_dir) != []:
            loading_status = self.load_vector_store(vector_store_save_dir)
            if loading_status:
                print(f"Vector store loaded from {vector_store_save_dir} successfully.")
            else:
                print(f"Failed to load vector store from {vector_store_save_dir}. Exit.")
        else:
            # Load local compilation instruction documents
            compilation_ins_doc_paths = self.get_local_documents_path()
            local_documents = self.load_documents(compilation_ins_doc_paths)
            documents.extend(local_documents)
            url_documents = []
            visited_urls = []
            # Load URL documents if enabled
            if self.include_urls:
                print("Info: Loading URL documents for RAG retrieval...")
                url_documents, visited_urls = self.load_url_documents()
                documents.extend(url_documents)
            
            print(f"Info: Total documents loaded for RAG retrieval: {len(documents)}")
            print(f"Info: Total URL documents loaded: {len(url_documents)}")
            print(f"Info: Total document tokens: {self.total_document_tokens}")
            print(f"Info: Total URL tokens: {self.total_url_tokens}")

            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=3000, chunk_overlap=200, add_start_index=True
            )
            all_splits = text_splitter.split_documents(documents)

            ### Save metadata
            self.save_metadata(compilation_ins_doc_paths, visited_urls)
            
            batch_size = 50  # Adjust based on your document sizes
            self.vector_store = Chroma(
                persist_directory=vector_store_save_dir,
                embedding_function=self.embeddings
            )
                        
            # Add documents in batches
            for i in range(0, len(all_splits), batch_size):
                batch = all_splits[i:i + batch_size]
                self.vector_store.add_documents(batch)
            print(f"Vector store created for {vector_store_save_dir} and documents added successfully.")

    def load_vector_store(self, vector_store_save_dir):
        try:
            self.vector_store = Chroma(persist_directory=vector_store_save_dir, embedding_function=self.embeddings)
            return True
        except Exception as e:
            print(f"Error loading vector store: {e}")
            return False


    def retrieve(self, query: str, k: int = 5):
        if not self.vector_store:
            print("Warning: Vector store is not loaded. Please load or create the vector store first.")
            return []
        
        retrieved_docs = self.vector_store.similarity_search(query, k=k)
        rag_output = ""
        for rank, doc in enumerate(retrieved_docs, start=1):
            rag_output += f"Rank {rank}: {doc.page_content}\n\n"

        return rag_output
    

if __name__ == '__main__':

    INCLUDE_URLS = True
    URL_VISIT_DEPTH = 3
    
    cloned_repo_dir = 'cloned_repos'
    data_path = "data/sampled_repos_149_cleaned_higher_split_compilable.jsonl"
    output_result_path = "data/rag_externl_urls_depth_3_k_3.json"

    total_document_tokens = 0
    total_url_tokens = 0
    
    all_retrieved_outputs = {}
    from tools import load_file
    github_repos = load_file(data_path)
    github_repos = github_repos['full_name'].to_list()
    for repo_full_name in tqdm(github_repos):
        repo_name = repo_full_name.split('/')[-1]
        repo_dir = os.path.join(cloned_repo_dir, repo_name)
        index_path = f'vector_store/{repo_name}_index'
        os.makedirs(index_path, exist_ok=True)
        rag_retrieval = RAGRetrieval(repo_dir=repo_dir, index_path=index_path, include_urls=INCLUDE_URLS, url_visit_depth=URL_VISIT_DEPTH)
        rag_retrieval.create_vector_store()
        query = f'“Build/compile instructions for {repo_full_name}: environment setup, required dependencies and versions, OS/toolchain notes, exact shell commands (configure/cmake/make/msbuild), and install step. Prefer README/INSTALL/BUILDING/docs/wiki pages; de-emphasize source files and tests.”'
        output = rag_retrieval.retrieve(query=query, k=3)

        all_retrieved_outputs[repo_name] = output

        total_document_tokens += rag_retrieval.total_document_tokens
        total_url_tokens += rag_retrieval.total_url_tokens
    print(f"Overall total document tokens: {total_document_tokens}")
    print(f"Overall total URL tokens: {total_url_tokens}")
    
    with open(output_result_path, 'w') as f:
        json.dump(all_retrieved_outputs, f, indent=4)
