import os
from library.filesystem import MOUNT_PATH
import stat
import errno
from functions.torboxFunctions import getDownloadLink, downloadFile
import time
import sys
import logging
from functions.appFunctions import getAllUserDownloads
import threading
from sys import platform
from collections import OrderedDict  # Added for LRU cache
import gc  # Added for explicit garbage collection
try:
    import psutil  # For memory usage logging
except ImportError:
    psutil = None

# Pull in some spaghetti to make this stuff work without fuse-py being installed
try:
    import _find_fuse_parts # type: ignore # noqa: F401
except ImportError:
    pass
import fuse
from fuse import Fuse
if not hasattr(fuse, '__version__'):
    raise RuntimeError("your fuse-python doesn't know of fuse.__version__, probably it's too old.")

fuse.fuse_python_api = (0, 2)

LINK_AGE = 3 * 60 * 60 # 3 hours

class VirtualFileSystem:
    def __init__(self, files_list):
        self.files = files_list
        self.structure = self._build_structure()
        self.file_map = self._build_file_map()

    def _build_structure(self):
        structure = {
            '/': ['movies', 'series'],
            '/movies': set(),
            '/series': set()
        }
        
        
        for f in self.files:
            media_type = f.get('metadata_mediatype')
            root_folder = f.get('metadata_rootfoldername')
            
            if media_type == 'movie':
                path = f'/movies/{root_folder}'
                structure['/movies'].add(root_folder)
                
                if path not in structure:
                    structure[path] = set()
                structure[path].add(f.get('metadata_filename'))
                
            elif media_type == 'series':
                path = f'/series/{root_folder}'
                structure['/series'].add(root_folder)
                
                if path not in structure:
                    structure[path] = set()
                structure[path].add(f.get('metadata_foldername'))
                
                season_path = f'{path}/{f.get("metadata_foldername")}'
                if season_path not in structure:
                    structure[season_path] = set()
                structure[season_path].add(f.get('metadata_filename'))
        
        # consistent ordering
        for key in structure:
            structure[key] = sorted([item for item in structure[key] if item is not None])
            
        return structure

    def _build_file_map(self):
        file_map = {}
        
        for f in self.files:
            if f.get('metadata_mediatype') == 'movie':
                path = f'/movies/{f.get("metadata_rootfoldername")}/{f.get("metadata_filename")}'
                file_map[path] = f
            else:  # series
                path = f'/series/{f.get("metadata_rootfoldername")}/{f.get("metadata_foldername")}/{f.get("metadata_filename")}'
                file_map[path] = f
                
        return file_map

    def is_dir(self, path):
        return path in self.structure
        
    def is_file(self, path):
        return path in self.file_map
        
    def get_file(self, path):
        return self.file_map.get(path)
        
    def list_dir(self, path):
        return self.structure.get(path, [])
    
class FuseStat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0

class TorBoxMediaCenterFuse(Fuse):
    def __init__(self, *args, **kwargs):
        super(TorBoxMediaCenterFuse, self).__init__(*args, **kwargs)

        threading.Thread(target=self.getFiles, daemon=True).start()

        self.files = []
        self.vfs = VirtualFileSystem(self.files)
        self.file_handles = {}
        self.next_handle = 1
        # Use OrderedDict for LRU cache
        self.cached_links = OrderedDict()
        self.cache = OrderedDict()
        self.block_size = 1024 * 1024 * 16
        self.max_blocks_per_link = 32
        self.max_blocks = 128  # Total blocks in cache
        self.max_linsks = 16  # Max links in cache

    def getFiles(self):
        while True:
            files = getAllUserDownloads()
            if files:
                self.files = files
                self.vfs = VirtualFileSystem(self.files)
                logging.debug(f"Updated {len(self.files)} files in VFS")
            time.sleep(300)
        
    def getattr(self, path):
        st = FuseStat()
        now = int(time.time())
        st.st_atime = now
        st.st_mtime = now
        st.st_ctime = now
        
        st.st_uid = os.getuid()
        st.st_gid = os.getgid()
        
        if self.vfs.is_dir(path):
            st.st_mode = stat.S_IFDIR | 0o755
            st.st_nlink = 2
            return st
        elif self.vfs.is_file(path):
            file_info = self.vfs.get_file(path)
            st.st_mode = stat.S_IFREG | 0o444
            st.st_nlink = 1
            # logging.debug(f"File info: {file_info}")
            st.st_atime = file_info.get('updated_at', now)
            st.st_mtime = file_info.get('updated_at', now)
            st.st_ctime = file_info.get('created_at', now)
            st.st_size = file_info.get('file_size', 0)
            return st
            
        # Not found
        return -errno.ENOENT
    
    def readdir(self, path, _):
        if not self.vfs.is_dir(path):
            return -errno.ENOENT
            
        yield fuse.Direntry('.')
        yield fuse.Direntry('..')
        
        for item in self.vfs.list_dir(path):
            yield fuse.Direntry(item)
    
    def open(self, _, flags):
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if (flags & accmode) != os.O_RDONLY:
            return -errno.EACCES
    
    def log_memory_usage(self, context=""):
        if psutil:
            process = psutil.Process(os.getpid())
            mem_info = process.memory_info()
            logging.debug(f"[MEMORY] {context} RSS: {mem_info.rss / (1024*1024):.2f} MB, VMS: {mem_info.vms / (1024*1024):.2f} MB")
        else:
            logging.debug(f"[MEMORY] {context} psutil not installed.")

    def read(self, path, size, offset):
        self.log_memory_usage("Before read")
        logging.debug(f"READ Path: {path}")
        logging.debug(f"READ Size: {size}")
        logging.debug(f"READ Offset: {offset}")
        file = self.vfs.get_file(path)

        current_time = time.time()
        if path not in self.cached_links:
            self.cached_links[path] = {
                'link': getDownloadLink(file.get('download_link')),
                'timestamp': current_time
            }
        elif current_time - self.cached_links[path]['timestamp'] > LINK_AGE:
            download_link = getDownloadLink(file.get('download_link'))
            self.cached_links[path] = {
                'link': download_link,
                'timestamp': current_time
            }
        download_link = self.cached_links[path]['link']

        # Enforce max_links for cached_links
        while len(self.cached_links) > self.max_links:
            old_link, _ = self.cached_links.popitem(last=False)
            # Remove all cache blocks for this link
            keys_to_remove = [k for k in self.cache.keys() if k[0] == old_link]
            for key in keys_to_remove:
                del self.cache[key]
            eviction_events.append(f"link")
        
        start_block = offset // self.block_size
        end_block = (offset + size - 1) // self.block_size

        buffer = bytearray()

        for block_index in range(start_block, end_block + 1):
            block_offset = block_index * self.block_size
            block_end = min((block_index + 1) * self.block_size - 1, file.get('file_size') - 1)
            current_block_size = block_end - block_offset + 1

            cache_key = (path, block_index)
            # check for block
            if cache_key not in self.cache:
                logging.debug(f"Cache miss for block {block_index}, fetching...")
                # get block
                block_data = downloadFile(download_link, current_block_size, block_offset)
                if not block_data:
                    return -errno.EIO
                # save block to cache
                self.cache[cache_key] = block_data
            else:
                # Move to end to mark as recently used
                self.cache.move_to_end(cache_key)
                block_data = self.cache[cache_key]

            logging.debug(f"Cache: {len(self.cache)} blocks, max {self.max_blocks_per_link * len(self.cached_links)} blocks ({self.max_blocks_per_link} per link), max {self.max_blocks} blocks")
            # LRU eviction for blocks per link
            # Count blocks for this link
            link_blocks = [k for k in self.cache.keys() if k[0] == path]
            while len(link_blocks) > self.max_blocks_per_link:
                # Remove least recently used block for this link
                for k in list(self.cache.keys()):
                    if k[0] == path:
                        del self.cache[k]
                        break
                link_blocks = [k for k in self.cache.keys() if k[0] == path]
                eviction_events.append(f"per-link block")

            # LRU eviction for total blocks
            while len(self.cache) > self.max_blocks:
                _, evicted_block = self.cache.popitem(last=False)
                del evicted_block  # Explicitly delete reference
                eviction_events.append(f"total block")

            start_offset_in_block = max(0, offset - block_offset)
            end_offset_in_block = min(len(block_data), offset + size - block_offset)

            view = memoryview(block_data)[start_offset_in_block:end_offset_in_block]
            buffer.extend(view)
            # No need to explicitly delete block_data

        if len(eviction_events) > 0:
            logging.debug(f"Eviction events: {', '.join(eviction_events)}")
            logging.debug(f"After eviction: {len(self.cache)} blocks, max {self.max_blocks_per_link * len(self.cached_links)} blocks ({self.max_blocks_per_link} per link), max {self.max_blocks} blocks")
            self.log_memory_usage("After eviction")
            gc.collect()  # Explicitly collect garbage after eviction
        # self.log_memory_usage("After read")
        return bytes(buffer)
    
    def release(self, _, fh):
        if fh in self.file_handles:
            del self.file_handles[fh]
        return 0
    
def runFuse():
    server = TorBoxMediaCenterFuse(
        version="%prog " + fuse.__version__,
        usage="%prog [options] mountpoint",
        dash_s_do="setsingle",
    )

    server.parser.add_option(
        mountopt="root",
        metavar="PATH",
        default=MOUNT_PATH,
        help="Mount point for the filesystem",
    )
    if platform != "darwin":
        server.fuse_args.add(
            "nonempty"
        )
    server.fuse_args.add(
        "allow_other"
    )
    # server.fuse_args.add(
    #     "allow_root"
    # )
    server.fuse_args.add(
        "-f"
    )
    server.parse(values=server, errex=1)
    try:
        server.fuse_args.mountpoint = MOUNT_PATH
    except OSError as e:
        logging.error(f"Error changing directory: {e}")
        sys.exit(1)
    server.main()

def unmountFuse():
    try:
        os.system("fusermount -u " + MOUNT_PATH)
    except OSError as e:
        logging.error(f"Error unmounting: {e}")
        sys.exit(1)
    logging.info("Unmounted successfully.")