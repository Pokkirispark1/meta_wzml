#!/usr/bin/env python3
from os import walk, path as ospath
import tempfile
from aiofiles.os import remove as aioremove, path as aiopath, listdir, rmdir, makedirs
from aioshutil import rmtree as aiormtree, move
from shutil import rmtree, disk_usage
from json import loads as jsonloads
from magic import Magic
from asyncio import create_subprocess_exec, gather, sleep
from asyncio.subprocess import PIPE
from re import split as re_split, I, search as re_search
from re import findall
from subprocess import run as srun
from sys import exit as sexit
from time import time

from .exceptions import NotSupportedExtractionArchive
from bot import bot_cache, aria2, LOGGER, DOWNLOAD_DIR, get_client, GLOBAL_EXTENSION_FILTER
from bot.helper.ext_utils.bot_utils import sync_to_async, cmd_exec

ARCH_EXT = [".tar.bz2", ".tar.gz", ".bz2", ".gz", ".tar.xz", ".tar", ".tbz2", ".tgz", ".lzma2",
            ".zip", ".7z", ".z", ".rar", ".iso", ".wim", ".cab", ".apm", ".arj", ".chm",
            ".cpio", ".cramfs", ".deb", ".dmg", ".fat", ".hfs", ".lzh", ".lzma", ".mbr",
            ".msi", ".mslz", ".nsis", ".ntfs", ".rpm", ".squashfs", ".udf", ".vhd", ".xar"]

FIRST_SPLIT_REGEX = r'(\.|_)part0*1\.rar$|(\.|_)7z\.0*1$|(\.|_)zip\.0*1$|^(?!.*(\.|_)part\d+\.rar$).*\.rar$'

SPLIT_REGEX = r'\.r\d+$|\.7z\.\d+$|\.z\d+$|\.zip\.\d+$'


def is_first_archive_split(file):
    return bool(re_search(FIRST_SPLIT_REGEX, file))


def is_archive(file):
    return file.endswith(tuple(ARCH_EXT))


def is_archive_split(file):
    return bool(re_search(SPLIT_REGEX, file))


async def clean_target(path):
    if await aiopath.exists(path):
        LOGGER.info(f"Cleaning Target: {path}")
        if await aiopath.isdir(path):
            try:
                await aiormtree(path)
            except Exception:
                pass
        elif await aiopath.isfile(path):
            try:
                await aioremove(path)
            except Exception:
                pass


async def clean_download(path):
    if await aiopath.exists(path):
        LOGGER.info(f"Cleaning Download: {path}")
        try:
            await aiormtree(path)
        except Exception:
            pass


async def start_cleanup():
    get_client().torrents_delete(torrent_hashes="all")
    try:
        await aiormtree(DOWNLOAD_DIR)
    except Exception:
        pass
    await makedirs(DOWNLOAD_DIR, exist_ok=True)


def clean_all():
    aria2.remove_all(True)
    get_client().torrents_delete(torrent_hashes="all")
    try:
        rmtree(DOWNLOAD_DIR)
    except Exception:
        pass


def exit_clean_up(signal, frame):
    try:
        LOGGER.info(
            "Please wait, while we clean up and stop the running downloads")
        clean_all()
        srun(['pkill', '-9', '-f', f'gunicorn|{bot_cache["pkgs"][-1]}'])
        sexit(0)
    except KeyboardInterrupt:
        LOGGER.warning("Force Exiting before the cleanup finishes!")
        sexit(1)


async def clean_unwanted(path):
    LOGGER.info(f"Cleaning unwanted files/folders: {path}")
    for dirpath, _, files in await sync_to_async(walk, path, topdown=False):
        for filee in files:
            if filee.endswith(".!qB") or filee.endswith('.parts') and filee.startswith('.'):
                await aioremove(ospath.join(dirpath, filee))
        if dirpath.endswith((".unwanted", "splited_files_mltb", "copied_mltb")):
            await aiormtree(dirpath)
    for dirpath, _, files in await sync_to_async(walk, path, topdown=False):
        if not await listdir(dirpath):
            await rmdir(dirpath)


async def get_path_size(path):
    if await aiopath.isfile(path):
        return await aiopath.getsize(path)
    total_size = 0
    for root, dirs, files in await sync_to_async(walk, path):
        for f in files:
            abs_path = ospath.join(root, f)
            total_size += await aiopath.getsize(abs_path)
    return total_size


async def count_files_and_folders(path):
    total_files = 0
    total_folders = 0
    for _, dirs, files in await sync_to_async(walk, path):
        total_files += len(files)
        for f in files:
            if f.endswith(tuple(GLOBAL_EXTENSION_FILTER)):
                total_files -= 1
        total_folders += len(dirs)
    return total_folders, total_files


def get_base_name(orig_path):
    extension = next(
        (ext for ext in ARCH_EXT if orig_path.lower().endswith(ext)), ''
    )
    if extension != '':
        return re_split(f'{extension}$', orig_path, maxsplit=1, flags=I)[0]
    else:
        raise NotSupportedExtractionArchive(
            'File format not supported for extraction')


def get_mime_type(file_path):
    mime = Magic(mime=True)
    mime_type = mime.from_file(file_path)
    mime_type = mime_type or "text/plain"
    return mime_type


def check_storage_threshold(size, threshold, arch=False, alloc=False):
    free = disk_usage(DOWNLOAD_DIR).free
    if not alloc:
        if (not arch and free - size < threshold or arch and free - (size * 2) < threshold):
            return False
    elif not arch:
        if free < threshold:
            return False
    elif free - size < threshold:
        return False
    return True


async def join_files(path):
    files = await listdir(path)
    results = []
    for file_ in files:
        if re_search(r"\.0+2$", file_) and await sync_to_async(get_mime_type, f'{path}/{file_}') == 'application/octet-stream':
            final_name = file_.rsplit('.', 1)[0]
            cmd = f'cat {path}/{final_name}.* > {path}/{final_name}'
            _, stderr, code = await cmd_exec(cmd, True)
            if code != 0:
                LOGGER.error(f'Failed to join {final_name}, stderr: {stderr}')
            else:
                results.append(final_name)
        else:
            LOGGER.warning('No Binary files to join!')
    if results:
        LOGGER.info('Join Completed!')
        for res in results:
            for file_ in files:
                if re_search(fr"{res}\.0[0-9]+$", file_):
                    await aioremove(f'{path}/{file_}')

async def add_attachment(listener, base_dir: str, media_file: str, outfile: str, attach: str = ''):
    attachment_ext = attach.split(".")[-1].lower()
    if attachment_ext in ["jpg", "jpeg"]:
        mime_type = "image/jpeg"
    elif attachment_ext == "png":
        mime_type = "image/png"
    else:
        mime_type = "application/octet-stream"

    cmd = [
        bot_cache['pkgs'][2], '-hide_banner', '-ignore_unknown', '-i', media_file, '-attach', attach, 
        '-metadata:s:t', f'mimetype={mime_type}', '-c', 'copy', '-map', '0', outfile, '-y'
    ]

    listener.suproc = await create_subprocess_exec(*cmd, stderr=PIPE)
    code = await listener.suproc.wait()

    if code == 0:
        await clean_target(media_file)
        listener.seed = False
        await move(outfile, base_dir)
    else:
        # Capture the stderr output and decode it properly
        stderr_output = await listener.suproc.stderr.read()  # Await first
        LOGGER.error('%s. Adding Attachment failed, Path %s', stderr_output.decode(), media_file)  # Then decode
        await clean_target(outfile)
 
def modify_srt(input_srt: str, output_srt: str, intro_text: str):
    """
    Modify an SRT file by adding an intro subtitle and renumbering existing entries
    without shifting their original timestamps.
    """
    try:
        with open(input_srt, 'r', encoding='utf-8') as f:
            content = f.read()
        blocks = content.strip().split('\n\n')
        intro_block = f'1\n00:00:00,000 --> 00:00:05,000\n{intro_text}\n'
        modified_blocks = [intro_block]
        for i, block in enumerate(blocks, start=2):
            lines = block.split('\n')
            if lines and lines[0].strip().isdigit():
                lines[0] = str(i)  # Renumber the subtitle index
                modified_blocks.append('\n'.join(lines))
        with open(output_srt, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(modified_blocks) + '\n')
    except Exception as e:
        LOGGER.error(f'Failed to modify SRT {input_srt}: {str(e)}')
        raise

async def edit_metadata(listener, base_dir: str, media_file: str, outfile: str, metadata: str = 'Moviemania', intro_text: str = '@Moviemania Intro'):
    """
    Edit a media file to add a 5-second intro subtitle and update metadata,
    preserving all tracks, attachments, and original subtitle timings.
    
    Args:
        listener: Object to track subprocess and status
        base_dir: Directory to move the output file
        media_file: Input video file path
        outfile: Output video file path
        metadata: Metadata title (default: 'Moviemania')
        intro_text: Text for the intro subtitle (default: '@Moviemania Intro')
    """
    ffprobe = bot_cache['pkgs'][1]  # Path to ffprobe
    ffmpeg = bot_cache['pkgs'][2]   # Path to ffmpeg

    ### Step 1: Probe the video for subtitle streams
    cmd_probe = [
        ffprobe, '-v', 'error', '-select_streams', 's',
        '-show_entries', 'stream=index:stream_tags=language',
        '-of', 'csv=p=0', media_file
    ]
    proc_probe = await create_subprocess_exec(*cmd_probe, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc_probe.communicate()
    if proc_probe.returncode != 0:
        LOGGER.error(f'ffprobe failed: {stderr.decode()}')
        return

    # Parse subtitle stream info
    subtitle_streams = []
    for line in stdout.decode().strip().split('\n'):
        if line:
            parts = line.split(',')
            index = int(parts[0])
            language = parts[1] if len(parts) > 1 else 'und'  # Default to 'und' (undefined) if no language
            subtitle_streams.append((index, language))

    temp_files = []
    try:
        if subtitle_streams:
            ### Case 1: Modify each existing subtitle track
            modified_srts = []
            for stream_index, language in subtitle_streams:
                temp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False).name
                mod_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False).name
                temp_files.extend([temp_srt, mod_srt])

                # Extract subtitle stream to SRT
                cmd_extract = [
                    ffmpeg, '-i', media_file, '-map', f'0:s:{stream_index}',
                    '-c:s', 'srt', temp_srt, '-y'
                ]
                proc_extract = await create_subprocess_exec(*cmd_extract, stderr=PIPE)
                code = await proc_extract.wait()
                if code != 0:
                    LOGGER.error(f'Failed to extract subtitle: {(await proc_extract.stderr.read()).decode()}')
                    return

                # Add intro subtitle and renumber without shifting timestamps
                modify_srt(temp_srt, mod_srt, intro_text)
                modified_srts.append((mod_srt, language))
        else:
            ### Case 2: Create a new subtitle track with only the intro text
            new_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False).name
            temp_files.append(new_srt)
            with open(new_srt, 'w', encoding='utf-8') as f:
                f.write(f'1\n00:00:00,000 --> 00:00:05,000\n{intro_text}\n\n')
            modified_srts = [(new_srt, 'und')]

        ### Step 2: Build FFmpeg command to remux with modified subtitles
        cmd = [ffmpeg, '-hide_banner', '-i', media_file]
        for srt, _ in modified_srts:
            cmd.extend(['-i', srt])  # Add each modified SRT as an input

        # Map streams: video, audio, attachments, and new/modified subtitles
        cmd.extend(['-map', '0:v:0?', '-map', '0:a:?', '-map', '0:t?'])
        for i in range(len(modified_srts)):
            cmd.extend(['-map', f'{i+1}:0'])

        # Copy codecs to avoid re-encoding
        cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-c:s', 'copy', '-c:t', 'copy'])

        # Set metadata for file and streams
        cmd.extend([
            '-metadata', f'title={metadata}',
            '-metadata:s:v', f'title={metadata}',
            '-metadata:s:a', f'title={metadata}'
        ])
        for i, (_, language) in enumerate(modified_srts):
            cmd.extend([
                f'-metadata:s:s:{i}', f'title={metadata}',
                f'-metadata:s:s:{i}', f'language={language}'  # Preserve original language tags
            ])
        cmd.extend([outfile, '-y'])

        ### Step 3: Execute FFmpeg command
        listener.suproc = await create_subprocess_exec(*cmd, stderr=PIPE)
        code = await listener.suproc.wait()
        if code == 0:
            # Success: Clean up original file and move output
            await clean_target(media_file)
            listener.seed = False
            await move(outfile, base_dir)
        else:
            # Failure: Clean up output file and log error
            await clean_target(outfile)
            LOGGER.error(f'Editing failed: {(await listener.suproc.stderr.read()).decode()}, Path {media_file}')

    finally:
        ### Clean up temporary files
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception as e:
                LOGGER.error(f'Failed to remove temp file {temp_file}: {str(e)}') 
                    
async def get_media_info(path: str):
    try:
        result = await cmd_exec(['ffprobe', '-hide_banner', '-loglevel', 'error', '-print_format', 'json', '-show_format', path])
        if res := result[1]:
            LOGGER.warning('Get Media Info: %s', res)
    except Exception as e:
        LOGGER.error('Get Media Info: %s. Mostly File not found!', e)
        return 0, None, None
    if result[0] and result[2] == 0:
        fields = jsonloads(result[0]).get('format')
        if fields is None:
            LOGGER.error('Get_media_info: %s', result)
            return 0, None, None
        duration = round(float(fields.get('duration', 0)))
        tags = fields.get('tags', {})
        artist = tags.get('artist') or tags.get('ARTIST') or tags.get('Artist')
        title = tags.get('title') or tags.get('TITLE') or tags.get('Title')
        return duration, artist, title
    return 0, None, None

class FFProgress:
    def __init__(self):
        self.outfile = ''
        self._duration = 0
        self._start_time = time()
        self._eta = 0
        self._percentage = '0%'
        self._processed_bytes = 0
    @property
    def processed_bytes(self):
        return self._processed_bytes
    @property
    def percentage(self):
        return self._percentage
    @property
    def eta(self):
        return self._eta
    @property
    def speed(self):
        return self._processed_bytes / (time() - self._start_time)
    @staticmethod
    async def read_lines(stream):
        data = bytearray()
        while not stream.at_eof():
            lines = re_split(br'[\r\n]+', data)
            data[:] = lines.pop(-1)
            for line in lines:
                yield line
            data.extend(await stream.read(1024))
    async def progress(self, status: str=''):
        start_time = time()
        async for line in self.read_lines(self.listener.suproc.stderr):
            if self.listener.suproc.returncode is not None:
                return
            if progress := dict(findall(r'(frame|fps|size|time|bitrate|speed)\s*\=\s*(\S+)', line.decode('utf-8').strip())):
                if not self._duration:
                    self._duration = (await get_media_info(self.path))[0]
                hh, mm, sms = progress['time'].split(':')
                time_to_second = (int(hh) * 3600) + (int(mm) * 60) + float(sms)
                self._processed_bytes = int(re_search(r'\d+', progress['size']).group()) * 1024
                self._percentage = f'{round((time_to_second / self._duration) * 100, 2)}%'
                try:
                    self._eta = (self._duration / float(progress['speed'].strip('x'))) - (time() - start_time)
                except:
                    pass
class Watermark(FFProgress):
    def __init__(self, listener):
        self.listener = listener
        self.path = ''
        self.name = ''
        self.size = 0
        self._start_time = time()
        super().__init__()
    async def add_watermark(self, media_file: str, wm_position: str, wm_size: str):
        self.path = media_file
        self.size = await get_path_size(media_file)
        base_file, _ = ospath.splitext(media_file)
        self.outfile = f'{base_file}_.mkv'
        self.name = ospath.basename(self.outfile)
        cmd = [bot_cache['pkgs'][2], '-hide_banner', '-y', '-i', media_file, '-i', f'wm/{self.listener.user_id}.png', '-filter_complex',
            f"[1][0]scale2ref=w='iw*{wm_size}/100':h='ow/mdar'[wm][vid];[vid][wm]overlay={wm_position}",
            '-crf', '28', '-preset', 'ultrafast', '-map', '0:a:?', '-map', '0:s:?', '-c:a', 'copy', '-c:s', 'copy', self.outfile]
        self.listener.suproc = await create_subprocess_exec(*cmd, stderr=PIPE)
        _, code = await gather(self.progress(), self.listener.suproc.wait())
        if code == 0:
            await clean_target(media_file)
            self.listener.seed = False
            return self.outfile
        if code == -9:
            self.suproc = 'cancelled'
            return False
        await clean_target(self.outfile)
        LOGGER.error('%s. Watermarking failed, Path %s', (await self.listener.suproc.stderr.read()).decode(), media_file)
        return False