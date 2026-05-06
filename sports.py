import requests
import os
import concurrent.futures
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# --- CẤU HÌNH CHECKER ---
TIMEOUT = 5
MAX_WORKERS = 20

def check_channel(url):
    """Kiểm tra link stream sâu (Deep Check) để bắt các lỗi lẩn trốn"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*',
            'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
            'Connection': 'keep-alive',
            'Referer': 'https://google.com/' # Fake referer giúp bypass tường lửa
        }
        
        # BẬT allow_redirects=True để đi đến tận cùng link cuối (bắt bài 302 -> 403)
        response = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True)
        
        # 1. Nếu link cuối cùng trả về lỗi >= 400 (như 403, 404, 500) -> Chết
        if response.status_code >= 400:
            return url, False
            
        # 2. Bắt bài trả về 200 OK nhưng nội dung lại là trang HTML báo lỗi (Access Denied)
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            return url, False
            
        return url, True
        
    except requests.RequestException:
        return url, False

def update_playlist():
    # 1. Lấy URL M3U từ biến môi trường
    m3u_url = os.getenv('TV_M3U_SOURCE_URL')
    
    # Dùng cho lúc test chạy file trực tiếp ở máy tính
    if not m3u_url:
        print("CẢNH BÁO: Chưa set TV_M3U_SOURCE_URL, sử dụng link mặc định để test.")
        m3u_url = "https://iptv-org.github.io/iptv/categories/sports.m3u"
        
    # Cấu hình retry
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
        response = http.get(
            m3u_url,
            timeout=30,
            headers={'User-Agent': 'M3U-Playlist-Updater/1.0'}
        )
        response.raise_for_status()
        
        # 3. Phân tích nội dung M3U
        lines = response.text.splitlines()
        header = "#EXTM3U"
        channels = [] # Lưu trữ tuple (thông_tin_kênh, link_stream)
        
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
        print(f"Bắt đầu kiểm tra {len(unique_channels)} kênh duy nhất...\n")

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

        # 6. Tái tạo lại nội dung M3U (Chỉ lấy các kênh sống từ list đã lọc)
        valid_m3u_lines = [header]
        alive_count = 0
        
        for extinf, url in unique_channels:
            if check_results.get(url):
                if extinf:
                    valid_m3u_lines.append(extinf)
                valid_m3u_lines.append(url)
                alive_count += 1

        final_m3u_content = "\n".join(valid_m3u_lines) + "\n"

        # 7. Ghi ra file sports.m3u
        output_filename = "sports.m3u"
        output_path = os.path.join(os.getcwd(), output_filename)
        print(f"\nĐang ghi ra file: {output_path}")
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(final_m3u_content)
            
            if not os.path.exists(output_path):
                raise Exception(f"Thất bại khi tạo file {output_filename}")
                
            file_size = os.path.getsize(output_path)
            if file_size == 0:
                raise Exception(f"File {output_filename} bị rỗng!")
                
            print(f"✅ Đã cập nhật thành công {output_filename}")
            print(f"📊 Kênh sống thực sự: {alive_count}/{len(unique_channels)}")
            print(f"📦 Dung lượng: {file_size} bytes")
            
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
    update_playlist()
