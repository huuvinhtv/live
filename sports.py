import requests
import argparse
import signal
import os
import sys
import time
import subprocess
import logging
import shutil
import random
import json
import codecs
import csv
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from dataclasses import dataclass
from requests.adapters import HTTPAdapter


@dataclass
class ScanConfig:
    """Configuration for an IPTV playlist scan."""
    group_title: str | None = None
    timeout: int = 15
    extended_timeout: int | None = None
    split: bool = False
    rename: bool = False
    skip_screenshots: bool = False
    output_file: str | None = None
    channel_search: str | None = None
    channel_pattern: object | None = None
    proxy_list: list | None = None
    test_geoblock: bool = False
    profile_bitrate: bool = False
    ffmpeg_available: bool = True
    ffprobe_available: bool = True
    backoff: str = 'linear'
    retries: int = 6
    workers: int = 4
    insecure: bool = False


ACTIVE_SUBPROCESSES = set()
_subprocess_lock = threading.Lock()
cancel_event = threading.Event()

def print_header():
    header_text = """
\033[96m██╗██████╗ ████████╗██╗   ██╗     ██████╗██╗  ██╗███████╗ ██████╗██╗  ██╗███████╗██████╗   
██║██╔══██╗╚══██╔══╝██║   ██║    ██╔════╝██║  ██║██╔════╝██╔════╝██║ ██╔╝██╔════╝██╔══██╗  
██║██████╔╝   ██║   ██║   ██║    ██║     ███████║█████╗  ██║     █████╔╝ █████╗  ██████╔╝  
██║██╔═══╝    ██║   ╚██╗ ██╔╝    ██║     ██╔══██║██╔══╝  ██║     ██╔═██╗ ██╔══╝  ██╔══██╗  
██║██║        ██║    ╚████╔╝     ╚██████╗██║  ██║███████╗╚██████╗██║  ██╗███████╗██║  ██║  
╚═╝╚═╝        ╚═╝     ╚═══╝       ╚═════╝╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝  
\033[0m    
""" 
    print(header_text)
    print("\033[93mWelcome to the IPTV Stream Checker!\n\033[0m")
    print("\033[93mUse -h for help on how to use this tool.\033[0m")

def setup_logging(verbose_level):
    if verbose_level == 1:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    elif verbose_level >= 2:
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    else:
        logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

def terminate_process(process):
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            process.terminate()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        pass

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'nt':
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            pass

def cleanup_active_subprocesses():
    with _subprocess_lock:
        procs = list(ACTIVE_SUBPROCESSES)
    for process in procs:
        terminate_process(process)
    with _subprocess_lock:
        ACTIVE_SUBPROCESSES.clear()

def run_managed_subprocess(command, timeout):
    popen_kwargs = {
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE
    }
    if os.name == 'nt':
        creation_flag = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        if creation_flag:
            popen_kwargs['creationflags'] = creation_flag
    else:
        popen_kwargs['preexec_fn'] = os.setsid

    process = None
    try:
        process = subprocess.Popen(command, **popen_kwargs)
        with _subprocess_lock:
            ACTIVE_SUBPROCESSES.add(process)
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if process is not None:
            terminate_process(process)
        raise
    finally:
        if process is not None:
            with _subprocess_lock:
                ACTIVE_SUBPROCESSES.discard(process)

def handle_sigint(signum, frame):
    logging.info("Interrupt received, stopping...")
    cancel_event.set()
    cleanup_active_subprocesses()

signal.signal(signal.SIGINT, handle_sigint)

def get_video_bitrate(url):
    command = [
        'ffmpeg',
        '-rw_timeout', '5000000',  # Thêm socket timeout
        '-v', 'debug',
        '-user_agent', 'VLC/3.0.14',
        '-i', url,
        '-t', '10',
        '-f', 'null', '-'
    ]
    try:
        result = run_managed_subprocess(command, timeout=20)
        output = result.stderr.decode(errors='ignore')
        total_bytes = 0
        for line in output.splitlines():
            if "Statistics:" in line and "bytes read" in line:
                parts = line.split("bytes read")
                try:
                    size_str = parts[0].strip().split()[-1]
                    total_bytes = int(size_str)
                    break
                except (IndexError, ValueError):
                    continue
        if total_bytes <= 0:
            return "N/A"
        bitrate_kbps = (total_bytes * 8) / 1000 / 10
        return f"{round(bitrate_kbps)} kbps"
    except Exception:
        return "N/A"

def check_ffmpeg_availability():
    tool_status = {}
    for tool in ['ffmpeg', 'ffprobe']:
        available = False
        try:
            result = run_managed_subprocess([tool, '-version'], timeout=5)
            if result.returncode == 0:
                available = True
        except Exception:
            pass
        tool_status[tool] = available
    return tool_status

def test_with_proxy(url, proxy, timeout, retries=3):
    headers = {'User-Agent': 'VLC/3.0.14 LibVLC/3.0.14'}
    proxies = {'http': proxy, 'https': proxy}
    stream_extensions = ('.ts', '.m2ts', '.m4s', '.mp4', '.aac', '.m3u8')

    for attempt in range(max(1, retries)):
        try:
            with requests.get(url, stream=True, timeout=(5, timeout), headers=headers, proxies=proxies) as resp:
                if resp.status_code != 200:
                    continue
                content_type = resp.headers.get('Content-Type', '')
                lowered_type = content_type.lower()
                stream_path = urlparse(resp.url).path.lower()
                if (
                    lowered_type.startswith('video/')
                    or lowered_type.startswith('audio/')
                    or 'application/vnd.apple.mpegurl' in lowered_type
                    or 'application/x-mpegurl' in lowered_type
                    or 'application/octet-stream' in lowered_type
                    or 'application/mp4' in lowered_type
                    or stream_path.endswith(stream_extensions)
                ):
                    for chunk in resp.iter_content(1024 * 500):
                        if chunk:
                            return True
        except requests.RequestException:
            pass
        if attempt + 1 < max(1, retries):
            time.sleep(0.5 * (attempt + 1))
    return False

def load_proxy_list(proxy_file):
    proxies = []
    valid_proxies = []

    def validate_proxy_entry(proxy_value):
        if not proxy_value:
            return None, "entry is empty"
        candidate = proxy_value.strip()
        if not candidate:
            return None, "entry is empty"
        if '://' not in candidate:
            candidate = f"http://{candidate}"

        parsed = urlparse(candidate)
        scheme = parsed.scheme.lower()
        if scheme not in {'http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h'}:
            return None, f"unsupported proxy scheme '{parsed.scheme}'"
        if not parsed.hostname:
            return None, "missing proxy host"

        try:
            port = parsed.port
        except ValueError:
            return None, "invalid proxy port"
        if port is None or port < 1 or port > 65535:
            return None, "invalid proxy port"

        return f"{scheme}://{parsed.netloc}", None

    try:
        with open(proxy_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read().strip()
            try:
                proxy_data = json.loads(content)
                if isinstance(proxy_data, list):
                    for proxy in proxy_data:
                        if isinstance(proxy, dict):
                            ip = proxy.get('ip')
                            port = proxy.get('port')
                            if ip and port:
                                if 'protocols' in proxy and isinstance(proxy['protocols'], list):
                                    for protocol in proxy['protocols']:
                                        proxies.append(f"{protocol}://{ip}:{port}")
                                elif 'protocol' in proxy:
                                    proxies.append(f"{proxy.get('protocol', 'http')}://{ip}:{port}")
                                else:
                                    proxies.append(f"http://{ip}:{port}")
                        elif isinstance(proxy, str):
                            proxies.append(proxy)
            except json.JSONDecodeError:
                lines = content.split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        proxies.append(line)
    except Exception:
        pass

    for proxy in proxies:
        validated_proxy, error_message = validate_proxy_entry(proxy)
        if not error_message:
            valid_proxies.append(validated_proxy)

    return valid_proxies

def summarize_error(exc):
    msg = str(exc).lower()
    if isinstance(exc, requests.Timeout):
        return "Connection timed out"
    if isinstance(exc, requests.ConnectionError):
        if any(kw in msg for kw in ['dns', 'name or service not known', 'nodename nor servname', 'no such host', 'getaddrinfo failed']):
            return "DNS resolution failed"
        if 'ssl' in msg or 'tls' in msg or 'certificate' in msg or 'handshake' in msg:
            return "SSL/TLS error"
        if 'connection refused' in msg:
            return "Connection refused"
        return "Connection error"
    if isinstance(exc, requests.TooManyRedirects):
        return "Redirect loop"
    return str(exc)[:80]

# --- TÁCH CÁC HÀM PHỤ TRỢ RA KHỎI HÀM MAIN CHECK ĐỂ TỐI ƯU ---
def is_playlist(content_type, target_url):
    lowered_type = content_type.lower()
    lowered_path = urlparse(target_url.lower()).path
    return (
        'application/vnd.apple.mpegurl' in lowered_type
        or 'application/x-mpegurl' in lowered_type
        or lowered_path.endswith('.m3u8')
    )

def is_direct_stream(content_type, target_url):
    lowered_type = content_type.lower()
    lowered_path = urlparse(target_url.lower()).path
    stream_extensions = ('.ts', '.m2ts', '.m4s', '.mp4', '.aac')
    return (
        lowered_type.startswith('video/')
        or lowered_type.startswith('audio/')
        or 'application/octet-stream' in lowered_type
        or 'application/mp4' in lowered_type
        or lowered_path.endswith(stream_extensions)
    )

def read_stream(response, min_bytes, min_data_threshold):
    bytes_read = 0
    for chunk in response.iter_content(1024 * 128):  # 128KB chunks
        if not chunk:
            continue
        bytes_read += len(chunk)
        if bytes_read >= min_bytes:
            return 'Alive', response.url, None

    fallback_threshold = min_bytes if min_bytes >= min_data_threshold else max(32768, min_bytes // 2)
    if bytes_read >= fallback_threshold:
        return 'Alive', response.url, None
    return 'Dead', None, 'Insufficient data received'

def extract_next_url(base_url, playlist_body):
    def parse_resolution_pixels(res_val):
        if not res_val: return 0
        match = re.match(r'^\s*(\d+)\s*x\s*(\d+)\s*$', res_val, flags=re.IGNORECASE)
        return int(match.group(1)) * int(match.group(2)) if match else 0
        
    def parse_int(val):
        try: return max(0, int(val.strip())) if val else 0
        except: return 0

    saw_stream_inf = False
    pending_variant_attrs = None
    best_variant_url = None
    best_variant_score = None
    fallback_url = None

    for raw_line in playlist_body.splitlines():
        line = raw_line.strip()
        if not line: continue
        if line.startswith('#'):
            if line.upper().startswith('#EXT-X-STREAM-INF'):
                saw_stream_inf = True
                # Regex bắt thuộc tính M3U8 chuẩn
                attrs = {}
                payload = line.partition(':')[2]
                for match in re.finditer(r'([A-Z0-9-]+)=("([^"]*)"|([^,]*))', payload):
                    val = match.group(3) if match.group(3) is not None else match.group(4)
                    attrs[match.group(1)] = val
                pending_variant_attrs = attrs
            continue

        resolved_url = urljoin(base_url, line)
        if not saw_stream_inf: return resolved_url

        if pending_variant_attrs is not None:
            res_px = parse_resolution_pixels(pending_variant_attrs.get('RESOLUTION'))
            avg_bw = parse_int(pending_variant_attrs.get('AVERAGE-BANDWIDTH'))
            bw = parse_int(pending_variant_attrs.get('BANDWIDTH'))
            score = (1 if res_px else 0, res_px, avg_bw, bw)
            
            if best_variant_score is None or score > best_variant_score:
                best_variant_score = score
                best_variant_url = resolved_url
            pending_variant_attrs = None
        elif fallback_url is None:
            fallback_url = resolved_url

    return best_variant_url or fallback_url


def check_channel_status(url, timeout, retries=6, extended_timeout=None, proxy_list=None, test_geoblock=False, ffmpeg_available=True, backoff='linear', session=None):
    headers = {'User-Agent': 'VLC/3.0.14 LibVLC/3.0.14'}
    min_data_threshold = 1024 * 500  
    playlist_segment_threshold = 1024 * 128 
    max_playlist_depth = 4
    initial_timeout = 5
    retryable_http_statuses = {408, 425, 429, 500, 502, 503, 504}
    geoblock_statuses = {403, 451, 426}
    secondary_geoblock_statuses = {401, 423, 451}
    backoff_mode = (backoff or 'linear').strip().lower()
    
    def verify(target_url, current_timeout, depth, visited):
        if depth > max_playlist_depth:
            return 'Dead', None, 'Max playlist depth exceeded'
        normalized_url = target_url.split('#')[0]
        if normalized_url in visited:
            return 'Dead', None, 'Playlist loop detected'
        visited.add(normalized_url)
        playlist_text = None
        final_url = target_url
        http = session or requests
        try:
            with http.get(target_url, stream=True, timeout=(initial_timeout, current_timeout), headers=headers) as resp:
                if resp.status_code in retryable_http_statuses:
                    return 'Retry', None, f'HTTP {resp.status_code}'
                if resp.status_code in geoblock_statuses:
                    return 'Geoblocked', None, None
                if resp.status_code != 200:
                    if resp.status_code in secondary_geoblock_statuses:
                        return 'Geoblocked', None, None
                    return 'Dead', None, f'HTTP {resp.status_code}'

                content_type = resp.headers.get('Content-Type', '')
                final_url = resp.url
                
                if is_playlist(content_type, final_url):
                    playlist_text = resp.text
                elif is_direct_stream(content_type, final_url):
                    min_bytes = min_data_threshold if depth == 0 else playlist_segment_threshold
                    return read_stream(resp, min_bytes, min_data_threshold)
                else:
                    if content_type.lower().startswith('text/'):
                        return 'Dead', None, f'Unrecognized content type: {content_type}'
                    min_bytes = min_data_threshold if depth == 0 else playlist_segment_threshold
                    return read_stream(resp, min_bytes, min_data_threshold)
        except requests.ConnectionError as exc:
            return 'Retry', None, summarize_error(exc)
        except requests.Timeout as exc:
            return 'Retry', None, summarize_error(exc)
        except requests.RequestException as e:
            return 'Dead', None, summarize_error(e)

        if not playlist_text:
            return 'Dead', None, 'Empty playlist response'

        next_url = extract_next_url(final_url, playlist_text)
        if not next_url:
            return 'Dead', None, 'No media segments in playlist'
        return verify(next_url, current_timeout, depth + 1, visited)

    def get_retry_delay(attempt_index):
        if backoff_mode == 'none': return 0
        if backoff_mode == 'exponential': return min(2 ** attempt_index, 30)
        return min(attempt_index + 1, 10)

    def attempt_check(current_timeout):
        total_attempts = max(1, retries)
        last_reason = None
        for attempt in range(total_attempts):
            if cancel_event.is_set():
                return 'Dead', None, 'Cancelled'
            visited = set()
            status, stream_url, reason = verify(url, current_timeout, 0, visited)
            if status == 'Retry':
                last_reason = reason
                if attempt + 1 < total_attempts:
                    delay_seconds = get_retry_delay(attempt)
                    if delay_seconds > 0: time.sleep(delay_seconds)
                continue
            return status, stream_url, reason
        return 'Dead', None, last_reason or 'Max retries exceeded'

    status, stream_url, error_reason = attempt_check(timeout)

    if status == 'Dead' and extended_timeout:
        status, stream_url, error_reason = attempt_check(extended_timeout)

    if status == 'Geoblocked' and test_geoblock and proxy_list:
        for proxy in random.sample(proxy_list, min(3, len(proxy_list))):
            if test_with_proxy(url, proxy, timeout):
                return 'Geoblocked (Confirmed)', None, None
        return 'Geoblocked (Unconfirmed)', None, None

    if status == 'Alive' and ffmpeg_available:
        verification_url = stream_url or url
        try:
            command = [
                'ffmpeg', '-rw_timeout', '5000000', '-user_agent', headers['User-Agent'], '-i', verification_url, '-t', '5', '-f', 'null', '-'
            ]
            run_managed_subprocess(command, timeout=15)
        except Exception:
            pass

    return status, stream_url, error_reason

def build_screenshot_filename(output_path, channel_index, channel_name, max_length=200):
    illegal_chars_pattern = r'[\\/:*?"<>|]'
    windows_reserved_names = {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
    normalized_name = channel_name if channel_name else "channel"
    normalized_name = re.sub(illegal_chars_pattern, '-', normalized_name).strip().strip('.')
    normalized_name = re.sub(r'\s+', ' ', normalized_name) or "channel"
    if normalized_name.upper() in windows_reserved_names:
        normalized_name = f"{normalized_name}_channel"

    base_prefix = f"{channel_index}-"
    remaining_length = max(1, max_length - len(base_prefix))
    base_name = normalized_name[:remaining_length]
    candidate = f"{base_prefix}{base_name}"

    suffix_index = 1
    while os.path.exists(os.path.join(output_path, f"{candidate}.png")):
        suffix = f"_{suffix_index}"
        allowed_length = max(1, remaining_length - len(suffix))
        candidate = f"{base_prefix}{base_name[:allowed_length]}{suffix}"
        suffix_index += 1

    return candidate

def capture_frame(url, output_path, file_name):
    command = ['ffmpeg', '-y', '-rw_timeout', '10000000', '-i', url, '-frames:v', '1', os.path.join(output_path, f"{file_name}.png")]
    try:
        run_managed_subprocess(command, timeout=30)
        return True
    except Exception:
        return False

def get_detailed_stream_info(url, profile_bitrate=False):
    command = [
        'ffprobe', '-v', 'error',
        '-rw_timeout', '5000000', # Socket timeout chống treo
        '-analyzeduration', '15000000', '-probesize', '15000000',
        '-select_streams', 'v', '-show_entries',
        'stream=codec_name,width,height,r_frame_rate', '-of', 'json', url
    ]
    try:
        result = run_managed_subprocess(command, timeout=10)
        output = result.stdout.decode(errors='ignore')
        codec_name = "Unknown"
        width = height = 0
        fps = None
        probe_data = json.loads(output) if output else {}
        streams = probe_data.get('streams', []) if isinstance(probe_data, dict) else []

        selected_stream = None
        selected_pixels = -1
        for stream in streams:
            if not isinstance(stream, dict): continue
            w = int(stream.get('width') or 0)
            h = int(stream.get('height') or 0)
            pixel_count = w * h
            if pixel_count > selected_pixels:
                selected_pixels = pixel_count
                selected_stream = stream

        if selected_stream:
            codec_name = (selected_stream.get('codec_name') or "Unknown").upper()
            width = int(selected_stream.get('width') or 0)
            height = int(selected_stream.get('height') or 0)
            fps_data = selected_stream.get('r_frame_rate')
            if fps_data:
                try:
                    if '/' in fps_data:
                        num_str, den_str = fps_data.split('/', 1)
                        if float(den_str) > 0: fps = round(float(num_str) / float(den_str))
                    else:
                        fps = round(float(fps_data))
                except ValueError: pass
        else:
            try:
                audio_probe_cmd = [
                    'ffprobe', '-v', 'error', '-rw_timeout', '5000000',
                    '-analyzeduration', '15000000', '-probesize', '15000000',
                    '-select_streams', 'a', '-show_entries', 'stream=codec_type',
                    '-of', 'json', url
                ]
                audio_result = run_managed_subprocess(audio_probe_cmd, timeout=10)
                audio_data = json.loads(audio_result.stdout.decode(errors='ignore')) if audio_result.stdout else {}
                if audio_data.get('streams', []): return "Audio Only", "N/A", "Audio Only", None
            except Exception: pass

        resolution = "Unknown"
        if width > 0 and height > 0:
            if width >= 3840 and height >= 2160: resolution = "4K"
            elif width >= 1920 and height >= 1080: resolution = "1080p"
            elif width >= 1280 and height >= 720: resolution = "720p"
            else: resolution = "SD"

        video_bitrate = get_video_bitrate(url) if profile_bitrate else "N/A"
        return codec_name, video_bitrate, resolution, fps
    except Exception:
        return "Unknown", "Unknown", "Unknown", None

def format_stream_info(codec_name, video_bitrate, resolution, fps):
    resolution_display = f"{resolution}{fps}" if resolution != "Unknown" and fps else resolution
    components = [c for c in (resolution_display, codec_name) if c and c != "Unknown"]
    base_info = " ".join(components) if components else "Unknown"
    if video_bitrate and video_bitrate not in ("Unknown", "N/A"):
        return f"{base_info} ({video_bitrate})"
    return base_info

def get_audio_bitrate(url):
    command = [
        'ffprobe', '-v', 'error',
        '-rw_timeout', '5000000',
        '-analyzeduration', '15000000', '-probesize', '15000000',
        '-select_streams', 'a:0', '-show_entries',
        'stream=codec_name,bit_rate', '-of', 'default=noprint_wrappers=1', url
    ]
    try:
        result = run_managed_subprocess(command, timeout=10)
        output = result.stdout.decode()
        audio_bitrate = None
        codec_name = None
        for line in output.splitlines():
            if line.startswith("bit_rate="):
                val = line.split('=')[1]
                audio_bitrate = int(val) // 1000 if val.isdigit() else 'N/A'
            elif line.startswith("codec_name="):
                codec_name = line.split('=')[1].upper()
        return f"{audio_bitrate} kbps {codec_name}" if codec_name and audio_bitrate else "Unknown"
    except Exception:
        return "Unknown"

def check_label_mismatch(channel_name, resolution):
    channel_name_lower = channel_name.lower()
    mismatches = []
    if re.search(r'\b4k\b', channel_name_lower) or re.search(r'\buhd\b', channel_name_lower):
        if resolution != "4K": mismatches.append(f"\033[91mExpected 4K, got {resolution}\033[0m")
    elif re.search(r'\b1080p\b', channel_name_lower) or re.search(r'\bfhd\b', channel_name_lower):
        if resolution != "1080p": mismatches.append(f"\033[91mExpected 1080p, got {resolution}\033[0m")
    elif re.search(r'\bhd\b', channel_name_lower):
        if resolution not in ["1080p", "720p"]: mismatches.append(f"\033[91mExpected 720p or 1080p, got {resolution}\033[0m")
    elif resolution == "4K":
        mismatches.append(f"\033[91m4K channel not labeled as such\033[0m")
    return mismatches

# --- TỐI ƯU CÚ PHÁP REGEX ĐỂ BÓC TÁCH M3U SIÊU TỐC ---
def parse_extinf_metadata(extinf_line):
    if not extinf_line.startswith('#EXTINF'):
        return {}, "Unknown Channel"

    _, _, payload = extinf_line.partition(':')
    if not payload:
        return {}, "Unknown Channel"

    in_quotes = False
    split_index = -1
    for idx, char in enumerate(payload):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ',' and not in_quotes:
            split_index = idx
            break

    if split_index >= 0:
        metadata_payload = payload[:split_index]
        channel_name = payload[split_index + 1:].strip() or "Unknown Channel"
    else:
        metadata_payload = payload
        channel_name = "Unknown Channel"

    attributes = {}
    for match in re.finditer(r'([a-zA-Z0-9_-]+)="([^"]*)"', metadata_payload):
        attributes[match.group(1).lower()] = match.group(2).strip()

    return attributes, channel_name

def get_channel_name(extinf_line):
    return parse_extinf_metadata(extinf_line)[1]

def get_group_name(extinf_line):
    return parse_extinf_metadata(extinf_line)[0].get('group-title', "Unknown Group")

def get_channel_id(url):
    if not url: return "Unknown"
    return url.rsplit('/', 1)[-1].replace('.ts', '') or "Unknown"

def is_line_needed(line, group_title, pattern):
    if not line.startswith('#EXTINF'): return False
    if group_title and get_group_name(line).strip().lower() != group_title.strip().lower(): return False
    if pattern and not pattern.search(get_channel_name(line)): return False
    return True

def compile_channel_pattern(channel_search):
    if not channel_search: return None
    try: return re.compile(channel_search, flags=re.IGNORECASE)
    except re.error as exc: raise ValueError(f"Invalid channel search regex '{channel_search}': {exc}") from exc

_TRACKING_PARAMS = frozenset({
    'token', 'auth', 'key', 'sig', 'signature', 'expires', 'expire',
    'ts', 'timestamp', 'nonce', 'hash', 'h', 'tk', 'st', 'e',
    'utid', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content',
    'utm_term', 'fbclid', 'gclid', '_', 'cb', 'cachebuster', 'rand',
})

def normalize_url_for_hash(url):
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: sorted(v) for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        normalized_query = urlencode(filtered, doseq=True)
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, normalized_query, ''))
    except: return url

def url_resume_hash(url):
    normalized = normalize_url_for_hash(url)
    return hashlib.sha256(normalized.encode('utf-8', errors='replace')).hexdigest()[:16]

def extract_resume_identifier(entry_text):
    if not entry_text: return None, ""
    text = entry_text.strip()
    if '|' in text:
        parts = text.split('|', 1)
        return parts[0].strip(), parts[1].strip()
    if '://' in text:
        for token in reversed(text.split()):
            if '://' in token: return None, token.strip()
    return None, text

def load_processed_channels(log_file):
    processed_hashes = set()
    processed_urls = set()
    processed_channel_indices = {}
    last_index = 0
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                parts = line.rstrip('\n').split(' - ', 1)
                if len(parts) > 1:
                    parsed_index = None
                    index_source = parts[0].strip()
                    if index_source:
                        index_tokens = index_source.split()
                        if index_tokens and index_tokens[0].isdigit():
                            parsed_index = int(index_tokens[0])
                            last_index = max(last_index, parsed_index)
                    entry_hash, entry_url = extract_resume_identifier(parts[1].strip())
                    if entry_hash:
                        processed_hashes.add(entry_hash)
                        if parsed_index is not None:
                            processed_channel_indices[entry_hash] = max(processed_channel_indices.get(entry_hash, 0), parsed_index)
                    if entry_url:
                        processed_urls.add(entry_url)
                        if not entry_hash and parsed_index is not None:
                            processed_channel_indices[entry_url] = max(processed_channel_indices.get(entry_url, 0), parsed_index)
    return processed_hashes, processed_urls, last_index, processed_channel_indices

class CheckpointWriter:
    def __init__(self, log_file, flush_interval=0.25, flush_threshold=128):
        self._log_file = log_file
        self._flush_interval = flush_interval
        self._flush_threshold = flush_threshold
        self._buffer = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

    def write(self, entry):
        with self._lock:
            self._buffer.append(entry)
            now = time.monotonic()
            if len(self._buffer) >= self._flush_threshold or (now - self._last_flush) >= self._flush_interval:
                self._flush_locked()

    def _flush_locked(self):
        if not self._buffer: return
        try:
            with open(self._log_file, 'a', encoding='utf-8', errors='replace') as f:
                for entry in self._buffer: f.write(entry + "\n")
        except OSError: pass
        self._buffer.clear()
        self._last_flush = time.monotonic()

    def flush(self):
        with self._lock: self._flush_locked()

    def close(self):
        self.flush()

class UrlDeduplicator:
    def __init__(self):
        self._lock = threading.Lock()
        self._results = {}
        self._pending = {}

    def get_or_start(self, url):
        with self._lock:
            if url in self._results: return 'cached', self._results[url]
            if url in self._pending: return 'waiting', self._pending[url]
            event = threading.Event()
            self._pending[url] = event
            return 'check', None

    def set_result(self, url, result):
        with self._lock:
            self._results[url] = result
            event = self._pending.pop(url, None)
        if event: event.set()

    def get_result(self, url):
        with self._lock: return self._results.get(url)

def sanitize_csv_field(value):
    if value is None: return ""
    normalized = str(value).replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    if normalized.lstrip().startswith(('=', '+', '-', '@')): return "'" + normalized
    return normalized

def file_log_entry(f_output, playlist_file, current_channel, total_channels, group_name, channel_name, channel_id, status, codec_name, video_bitrate, resolution, fps, audio_info, error_reason=None):
    if f_output is None: return
    csv.writer(f_output, lineterminator='\n').writerow([
        sanitize_csv_field(playlist_file), current_channel, total_channels,
        sanitize_csv_field(status), sanitize_csv_field(group_name),
        sanitize_csv_field(channel_name), sanitize_csv_field(channel_id if channel_id else "Unknown"),
        sanitize_csv_field(codec_name if codec_name else "Unknown"),
        sanitize_csv_field(video_bitrate.replace("kbps", "").strip() if isinstance(video_bitrate, str) else video_bitrate or "Unknown"),
        sanitize_csv_field(resolution), fps if fps is not None else "",
        sanitize_csv_field(audio_info if audio_info else "Unknown"),
        sanitize_csv_field(error_reason) if error_reason else ""
    ])
    f_output.flush()

def console_log_entry(playlist_file, current_channel, total_channels, channel_name, status, video_info, audio_info, max_name_length, use_padding):
    if status == 'Alive': color, status_symbol = "\033[92m", '✓'
    elif 'Geoblocked' in status: color, status_symbol = "\033[93m", '🔒'
    else: color, status_symbol = "\033[91m", '✕'
    
    name_padding = ' ' * (max_name_length - len(channel_name) + 3) if use_padding else ''
    prefix = f"{playlist_file}| " if playlist_file else ""
    
    if status == 'Alive':
        print(f"{color}{prefix}{current_channel}/{total_channels} {status_symbol} {channel_name}{name_padding} | Video: {video_info} - Audio: {audio_info}\033[0m")
    elif 'Geoblocked' in status:
        info = f" [{status}]" if 'Confirmed' in status or 'Unconfirmed' in status else " [Geoblocked]"
        print(f"{color}{prefix}{current_channel}/{total_channels} {status_symbol} {channel_name}{name_padding} |{info}\033[0m" if use_padding else f"{color}{prefix}{current_channel}/{total_channels} {status_symbol} {channel_name}{info}\033[0m")
    else:
        print(f"{color}{prefix}{current_channel}/{total_channels} {status_symbol} {channel_name}{name_padding} |\033[0m" if use_padding else f"{color}{prefix}{current_channel}/{total_channels} {status_symbol} {channel_name}\033[0m")

def parse_m3u8_files(playlists, config):
    if not playlists: return

    session = requests.Session()
    session.headers.update({'User-Agent': 'VLC/3.0.14 LibVLC/3.0.14'})
    if config.insecure:
        session.verify = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # TỐI ƯU CỔ CHAI CONNECTION POOL THEO WORKERS
    pool_size = max(20, config.workers * 2)
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    group_suffix = config.group_title.replace('|', '').replace(' ', '') if config.group_title else 'AllGroups'
    pattern = config.channel_pattern

    console_width = shutil.get_terminal_size((80, 20)).columns
    low_framerate_channels = []
    mislabeled_channels = []
    geoblocked_summary = {}
    error_summary = {}
    url_dedup = UrlDeduplicator()

    f_output = None
    if config.output_file:
        os.makedirs(os.path.dirname(config.output_file) or '.', exist_ok=True)
        try:
            f_output = codecs.open(config.output_file, "w", "utf-8-sig")
            f_output.write("Playlist,Channel Number,Total Channels in Playlist,Channel Status,Group Name,Channel Name,Channel ID,Codec,Bit Rate (kbps),Resolution,Frame Rate,Audio,Error Reason\n")
        except Exception:
            f_output = None

    for file_path in playlists:
        playlist_file = os.path.basename(file_path)
        base_playlist_name = os.path.splitext(playlist_file)[0]
        playlist_dir = os.path.dirname(file_path) or '.'

        output_folder = None
        if not config.skip_screenshots:
            output_folder = os.path.join(playlist_dir, f"{base_playlist_name}_{group_suffix}_screenshots")
            os.makedirs(output_folder, exist_ok=True)

        log_file = os.path.join(playlist_dir, f"{base_playlist_name}_{group_suffix}_checklog.txt")
        processed_hashes, processed_urls, last_index, processed_channel_indices = load_processed_channels(log_file)
        open(log_file, 'w', encoding='utf-8').close()
        
        current_channel = last_index
        written_resume_entries = set()
        working_channels, dead_channels, geoblocked_channels = [], [], []
        total_channels, max_name_length = 0, 0
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as file:
                for line in file:
                    if is_line_needed(line.strip(), config.group_title, pattern):
                        total_channels += 1
                        max_name_length = max(max_name_length, len(get_channel_name(line)))
        except Exception: continue

        use_padding = max_name_length + 60 <= console_width
        renamed_lines = [] if config.rename else None
        pending_extinf, pending_channel_name = None, None
        pending_metadata_lines, pending_selected = [], False
        checkpoint_writer = CheckpointWriter(log_file)
        entries_to_check = []

        def write_resume_entry(stream_hash, stream_url, channel_index):
            if not stream_hash or stream_hash in written_resume_entries: return
            checkpoint_writer.write(f"{channel_index} - {stream_hash}|{stream_url}")
            written_resume_entries.add(stream_hash)

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as file:
                for line in file:
                    line = line.strip()
                    if pending_extinf is None:
                        if line.startswith('#EXTINF'):
                            pending_extinf, pending_channel_name = line, get_channel_name(line)
                            pending_selected = is_line_needed(line, config.group_title, pattern)
                            pending_metadata_lines = []
                        elif renamed_lines is not None: renamed_lines.append(line)
                        continue

                    if line.startswith('#EXTINF'):
                        if renamed_lines is not None: renamed_lines.extend([pending_extinf] + pending_metadata_lines)
                        pending_extinf, pending_channel_name = line, get_channel_name(line)
                        pending_selected = is_line_needed(line, config.group_title, pattern)
                        pending_metadata_lines = []
                        continue

                    if not line or line.startswith('#'):
                        pending_metadata_lines.append(line)
                        continue

                    if pending_selected:
                        stream_hash = url_resume_hash(line)
                        if stream_hash not in processed_hashes and line not in processed_urls:
                            current_channel += 1
                            entry = {
                                'channel_index': current_channel, 'extinf_line': pending_extinf,
                                'channel_name': pending_channel_name, 'metadata_lines': list(pending_metadata_lines),
                                'stream_line': line, 'group_value': get_group_name(pending_extinf),
                                'channel_id': get_channel_id(line), 'result': None
                            }
                            if renamed_lines is not None: entry['renamed_line_idx'] = len(renamed_lines)
                            entries_to_check.append(entry)
                            processed_hashes.add(stream_hash)
                        else:
                            resume_index = processed_channel_indices.get(stream_hash) or max(1, current_channel)
                            write_resume_entry(stream_hash, line, resume_index)

                    if renamed_lines is not None: renamed_lines.extend([pending_extinf] + pending_metadata_lines + [line])
                    pending_extinf, pending_selected, pending_metadata_lines = None, False, []
        except Exception: continue

        print_lock, diag_semaphore = threading.Lock(), threading.Semaphore(min(config.workers, 4))

        def check_channel_worker(check_entry):
            if cancel_event.is_set(): return {'status': 'Dead', 'error_reason': 'Cancelled'}
            s_line = check_entry['stream_line']
            action, cached = url_dedup.get_or_start(s_line)
            if action == 'cached': return cached
            if action == 'waiting':
                cached.wait()
                return url_dedup.get_result(s_line)

            result = None
            try:
                status, stream_url, check_reason = check_channel_status(
                    s_line, config.timeout, retries=config.retries, extended_timeout=config.extended_timeout,
                    proxy_list=config.proxy_list, test_geoblock=config.test_geoblock,
                    ffmpeg_available=config.ffmpeg_available, backoff=config.backoff, session=session
                )
                target_url = (stream_url or s_line) if status == 'Alive' else None
                codec_name, video_bitrate, resolution, fps, video_info, audio_info = "Unknown", "Unknown", "Unknown", None, "Unknown", "Unknown"

                if status == 'Alive' and config.ffprobe_available and target_url:
                    with diag_semaphore:
                        codec_name, video_bitrate, resolution, fps = get_detailed_stream_info(target_url, config.profile_bitrate and config.ffmpeg_available)
                        video_info = format_stream_info(codec_name, video_bitrate, resolution, fps)
                        audio_info = get_audio_bitrate(target_url)

                if status == 'Alive' and not config.skip_screenshots and output_folder and config.ffmpeg_available:
                    with diag_semaphore:
                        capture_frame(target_url or s_line, output_folder, build_screenshot_filename(output_folder, check_entry['channel_index'], check_entry['channel_name']))

                result = {'status': status, 'stream_url': stream_url, 'target_url': target_url, 'video_info': video_info, 'audio_info': audio_info, 'codec_name': codec_name, 'video_bitrate': video_bitrate, 'resolution': resolution, 'fps': fps, 'error_reason': check_reason}
            except Exception as e: result = {'status': 'Dead', 'error_reason': summarize_error(e)}
            finally:
                if result is not None: url_dedup.set_result(s_line, result)
            return result

        cancelled = False
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            future_map = {executor.submit(check_channel_worker, e): e for e in entries_to_check}
            try:
                for future in as_completed(future_map):
                    if cancel_event.is_set():
                        for p in future_map: p.cancel()
                        cancelled = True; break
                    
                    check_entry = future_map[future]
                    result = future.result()
                    check_entry['result'] = result
                    status = result.get('status', 'Dead')

                    with print_lock:
                        if status == 'Alive' and config.ffprobe_available:
                            if mismatches := check_label_mismatch(check_entry['channel_name'], result.get('resolution', 'Unknown')):
                                mislabeled_channels.append(f"{playlist_file}: {check_entry['channel_index']}/{total_channels} {check_entry['channel_name']} - {', '.join(mismatches)}")
                            if result.get('fps') is not None and result['fps'] < 29:
                                low_framerate_channels.append(f"{playlist_file}: {check_entry['channel_index']}/{total_channels} {check_entry['channel_name']} - \033[91m{result['fps']}fps\033[0m")
                        
                        if 'Geoblocked' in status: geoblocked_summary[playlist_file] = geoblocked_summary.get(playlist_file, 0) + 1
                        elif status == 'Dead': 
                            r = result.get('error_reason') or 'Unknown'
                            error_summary[r] = error_summary.get(r, 0) + 1

                        console_log_entry(playlist_file, check_entry['channel_index'], total_channels, check_entry['channel_name'], status, result.get('video_info', ''), result.get('audio_info', ''), max_name_length, use_padding)
                        file_log_entry(f_output, playlist_file, check_entry['channel_index'], total_channels, check_entry['group_value'], check_entry['channel_name'], check_entry['channel_id'], status, result.get('codec_name', ''), result.get('video_bitrate', ''), result.get('resolution', ''), result.get('fps'), result.get('audio_info', ''), error_reason=result.get('error_reason'))
                        write_resume_entry(url_resume_hash(check_entry['stream_line']), check_entry['stream_line'], check_entry['channel_index'])
            except KeyboardInterrupt:
                cancel_event.set()
                for p in future_map: p.cancel()
                cancelled = True

        if cancelled:
            checkpoint_writer.close()
            cleanup_active_subprocesses()
            if f_output: f_output.close()
            session.close()
            sys.exit(130)

        for check_entry in entries_to_check:
            result = check_entry.get('result', {})
            status = result.get('status', 'Dead')
            output_extinf = check_entry['extinf_line']
            
            if status == 'Alive' and config.rename and renamed_lines is not None:
                extinf_parts = output_extinf.split(',', 1)
                if len(extinf_parts) > 1:
                    extinf_parts[1] = f"{check_entry['channel_name']} ({result.get('video_info')} | Audio: {result.get('audio_info')})"
                    output_extinf = ','.join(extinf_parts)
                if 'renamed_line_idx' in check_entry: renamed_lines[check_entry['renamed_line_idx']] = output_extinf

            if config.split:
                entry_lines = [output_extinf, *check_entry['metadata_lines'], check_entry['stream_line']]
                if status == 'Alive': working_channels.append(entry_lines)
                elif 'Geoblocked' in status: geoblocked_channels.append(entry_lines)
                else: dead_channels.append(entry_lines)

        checkpoint_writer.close()

        if config.split:
            for ch_list, suffix in [(working_channels, "_working.m3u8"), (dead_channels, "_dead.m3u8"), (geoblocked_channels, "_geoblocked.m3u8")]:
                if ch_list:
                    with open(os.path.join(playlist_dir, base_playlist_name + suffix), 'w', encoding='utf-8') as f:
                        f.write("#EXTM3U\n")
                        for entry in ch_list:
                            for line in entry: f.write(line + "\n")
        
        if config.rename and renamed_lines:
            with open(os.path.join(playlist_dir, f"{base_playlist_name}_renamed.m3u8"), 'w', encoding='utf-8') as f:
                if not any(e.upper().startswith("#EXTM3U") for e in renamed_lines if e): f.write("#EXTM3U\n")
                for line in renamed_lines: f.write(line + "\n")

    session.close()
    if f_output: f_output.close()

def main():
    print_header()
    parser = argparse.ArgumentParser(description="Check the status of channels in an IPTV M3U8 playlist and capture frames of live channels.")
    parser.add_argument("playlist", type=str, help="Path to the M3U8 playlist file")
    parser.add_argument("-group", "-g", type=str, default=None, help="Specific group title to check")
    parser.add_argument("-timeout", "-t", type=float, default=10.0, help="Timeout in seconds")
    parser.add_argument("-v", action="count", default=0, help="Increase output verbosity")
    parser.add_argument("-extended", "-e", type=int, nargs='?', const=10, default=None, help="Enable extended timeout check")
    parser.add_argument("-split", "-s", action="store_true", help="Create separate playlists")
    parser.add_argument("-rename", "-r", action="store_true", help="Rename alive channels to include info")
    parser.add_argument("-proxy-list", "-p", type=str, default=None, help="Path to proxy list file")
    parser.add_argument("-test-geoblock", "-tg", action="store_true", help="Test geoblocked streams with proxies")
    parser.add_argument("--retries", "-R", type=int, default=6, help="Number of stream-check attempts")
    parser.add_argument("-output", "-o", type=str, default=None, help="Write channel details to CSV")
    parser.add_argument("-channel_search", "-c", type=str, default=None, help="Regex used to filter channels")
    parser.add_argument("-skip_screenshots", action="store_true", help="Skip capturing screenshots")
    parser.add_argument("--profile-bitrate", "-b", action="store_true", help="Profile average video bitrate")
    parser.add_argument("--backoff", "-B", type=str, choices=["none", "linear", "exponential"], default="linear", help="Retry backoff strategy")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Number of concurrent workers (1-20)")
    parser.add_argument("--insecure", "-k", action="store_true", help="Disable SSL verification")

    args = parser.parse_args()
    try: channel_pattern = compile_channel_pattern(args.channel_search)
    except ValueError as exc: parser.error(str(exc))

    setup_logging(args.v)
    tool_status = check_ffmpeg_availability()
    ffmpeg_available = tool_status.get('ffmpeg', False)
    ffprobe_available = tool_status.get('ffprobe', False)
    
    if args.profile_bitrate and not ffmpeg_available: args.profile_bitrate = False
    proxy_list = load_proxy_list(os.path.expanduser(args.proxy_list)) if args.proxy_list else None

    playlist_input = os.path.expanduser(args.playlist)
    playlists = [os.path.join(playlist_input, e) for e in sorted(os.listdir(playlist_input)) if os.path.isfile(os.path.join(playlist_input, e)) and e.lower().endswith((".m3u", ".m3u8"))] if os.path.isdir(playlist_input) else [playlist_input] if os.path.isfile(playlist_input) else []

    if not playlists: return
    config = ScanConfig(group_title=args.group, timeout=args.timeout, extended_timeout=args.extended, split=args.split, rename=args.rename, skip_screenshots=args.skip_screenshots, output_file=os.path.expanduser(args.output) if args.output else None, channel_search=args.channel_search, channel_pattern=channel_pattern, proxy_list=proxy_list, test_geoblock=args.test_geoblock, profile_bitrate=args.profile_bitrate, ffmpeg_available=ffmpeg_available, ffprobe_available=ffprobe_available, backoff=args.backoff, retries=args.retries, workers=args.workers, insecure=args.insecure)
    parse_m3u8_files(playlists, config)

if __name__ == "__main__":
    main()
