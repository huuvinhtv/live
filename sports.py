import requests
import os
import sys
import concurrent.futures
from urllib.parse import urljoin # Dùng để nối link con với link mẹ
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from datetime import datetime

# --- CẤU HÌNH CHECKER ---
TIMEOUT = 5
MAX_WORKERS = 20
OUTPUT_FILENAME = "sports.m3u" # ⬅️ BẠN ĐỔI TÊN FILE M3U ĐẦU RA Ở ĐÂY

def check_channel(url):
    """Kiểm tra link stream sâu 2 tầng (Fix lỗi GZIP và Redirect 307)"""
    try:
        headers = {
            'User-Agent': 'VLC/3.0.16 LibVLC/3.0.16',
            'Accept': 'application/x-mpegURL, application/vnd.apple.mpegurl, */*',
            'Connection': 'keep-alive'
        }
        
        response = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True)
        
        # Vòng 1: Bắt lỗi HTTP lớp vỏ
        if response.status_code >= 400:
            response.close()
            return url, False
            
        # Vòng 2: Bắt lỗi qua Content-Type
        content_type = response.headers.get('Content-Type', '').lower()
        if 'html' in content_type or 'xml' in content_type:
            response.close()
            return url, False
            
        # --- VÒNG 3: FIX LỖI GZIP ---
        # Sử dụng iter_content để tự động giải nén dữ liệu từ CDN
        text_data = ""
        try:
            for chunk in response.iter_content(chunk_size=2048, decode_unicode=True):
                if chunk:
                    if isinstance(chunk, bytes):
                        text_data = chunk.decode('utf-8', errors='ignore')
                    else:
                        text_data = chunk
                    break # Chỉ lấy 1 chunk (2KB) đầu tiên là đủ phân tích
        except Exception:
            pass
        finally:
            response.close() 
            
        text_data = text_data.strip() # Cắt bỏ khoảng trắng và ký tự ẩn (BOM)
        
        # Vòng 4: Kiểm tra định dạng chuẩn M3U8
        if '.m3u8' in url or 'mpegurl' in content_type:
            # Tìm chữ #EXTM3U trong 50 ký tự đầu thay vì startswith để né ký tự ẩn
            if '#EXTM3U' not in text_data[:50]:
                return url, False
                
        # Bắt file XML/JSON báo lỗi ẩn danh
        if '<xml' in text_data[:50].lower() or '<error' in text_data[:50].lower() or text_data.startswith('{'):
            return url, False
            
        # Vòng 5: Bắt "Soft 404"
        text_data_lower = text_data.lower()
        error_keywords = ['404', 'not found', 'file not found', 'access denied', 'forbidden', 'banned', 'blocked', 'invalid token', 'error']
        for kw in error_keywords:
            if kw in text_data_lower[:200]: 
                return url, False

        # --- VÒNG 6 (TRÙM CUỐI): FIX LỖI REDIRECT 307 ---
        if '#EXTM3U' in text_data:
            lines = text_data.splitlines()
            inner_url = None
            
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    # QUAN TRỌNG: Dùng response.url (link đích sau khi bị chuyển hướng) để ghép nối
                    inner_url = urljoin(response.url, line)
                    break
            
            if inner_url:
                try:
                    inner_resp = requests.get(inner_url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True)
                    inner_status = inner_resp.status_code
                    inner_resp.close()
                    
                    if inner_status >= 400:
                        return url, False
                except requests.RequestException:
                    return url, False

        return url, True
        
    except requests.RequestException:
        return url, False


def update_playlist():
    # 1. Lấy URL M3U từ biến môi trường
    m3u_url = os.getenv('TV_M3U_SOURCE_URL')
    
    if not m3u_url:
        print("CẢNH BÁO: Chưa set TV_M3U_SOURCE_URL, sử dụng link mặc định để test.")
        m3u_url = "https://iptv-org.github.io/iptv/categories/sports.m3u"
        
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    http = requests.Session()
    http.mount("https://", adapter)
    http.mount("http://", adapter)

    try:
        # 2. Tải nội dung M3U
        print(f"Đang tải playlist từ: {m3u_url}...")
        
        # NGỤY TRANG THÀNH TRÌNH DUYỆT CHROME ĐỂ TRÁNH BỊ CHẶN (BLOCK) KHI TẢI LIST TỪ CÁC NGUỒN KHÓ TÍNH
        fetch_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        
        response = http.get(
            m3u_url,
            timeout=30,
            headers=fetch_headers
        )
        response.raise_for_status()
        
        # 3. Phân tích nội dung M3U
        lines = response.text.splitlines()
        header = "#EXTM3U"
        channels = [] 
        
        current_extinf = None
        for line in lines:
            line = line.strip()
            if line.startswith('#EXTM3U'):
                header = line
            elif line.startswith('#EXTINF'):
                current_extinf = line
            elif line.startswith('http'):
                channels.append((current_extinf, line))
                current_extinf = None

        print(f"Tổng số kênh ban đầu: {len(channels)}")

        # 4. Lọc bỏ các kênh trùng lặp link
        unique_channels = []
        seen_urls = set()
        for extinf, url in channels:
            if url not in seen_urls:
                seen_urls.add(url)
                unique_channels.append((extinf, url))
                
        print(f"Đã lọc bỏ {len(channels) - len(unique_channels)} kênh trùng lặp.")
        print(f"Bắt đầu kiểm tra sâu {len(unique_channels)} kênh bằng đa luồng...\n")

        # 5. Kiểm tra đa luồng các link
        urls = [c[1] for c in unique_channels]
        check_results = {}
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_url = {executor.submit(check_channel, url): url for url in urls}
            for future in concurrent.futures.as_completed(future_to_url):
                url, is_alive = future.result()
                check_results[url] = is_alive
                if is_alive:
                    print(f"[🟢 SỐNG] {url}")

        # 6. Tái tạo lại nội dung M3U và tạo HEADER
        alive_channels_list = [(extinf, url) for extinf, url in unique_channels if check_results.get(url)]
        alive_count = len(alive_channels_list)
        current_time = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        
        base_name = os.path.splitext(OUTPUT_FILENAME)[0].replace('_', ' ').replace('-', ' ').title()
        
        valid_m3u_lines = [
            "#=================================",
            f"# 📡 IPTV {base_name} Channels",
            f"# 🕒 Last Updated: {current_time}",
            f"# 📺 Channels Count : {alive_count}",
            "#==================================",
            header # Chữ #EXTM3U nằm sau phần Header trang trí
        ]
        
        # Thêm thông tin kênh vào file
        for extinf, url in alive_channels_list:
            if extinf:
                valid_m3u_lines.append(extinf)
            valid_m3u_lines.append(url)

        final_m3u_content = "\n".join(valid_m3u_lines) + "\n"

        # 7. Ghi ra file
        output_path = os.path.join(os.getcwd(), OUTPUT_FILENAME)
        print(f"\nĐang ghi ra file: {output_path}")
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(final_m3u_content)
            
            if not os.path.exists(output_path):
                raise Exception(f"Thất bại khi tạo file {OUTPUT_FILENAME}")
                
            file_size = os.path.getsize(output_path)
            if file_size == 0:
                raise Exception(f"File {OUTPUT_FILENAME} bị rỗng!")
                
            print(f"✅ Đã cập nhật thành công {OUTPUT_FILENAME}")
            print(f"📊 Kênh SỐNG/TỔNG: {alive_count}/{len(unique_channels)}")
            print(f"📦 Dung lượng file: {file_size} bytes")
            
        except Exception as e:
            print(f"Lỗi ghi file {output_path}: {str(e)}")
            if os.path.exists(output_path):
                os.remove(output_path)
            raise
            
        return True
        
    except Exception as e:
        print(f"Lỗi khi cập nhật M3U playlist: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = update_playlist()
    if not success:
        print("\n❌ LỖI: Quá trình cập nhật thất bại! Đang dừng GitHub Actions...")
        sys.exit(1) # Ép GitHub Actions báo lỗi đỏ và dừng lại
