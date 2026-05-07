import aiohttp
import asyncio
import os
import sys
from urllib.parse import urljoin
from datetime import datetime

# --- CẤU HÌNH CHECKER ---
TIMEOUT = 5
MAX_CONCURRENT = 100 # Với aiohttp, bạn có thể đẩy số lượng chạy song song lên 100-200 thoải mái
OUTPUT_FILENAME = "sports.m3u"

async def check_channel(sem, session, url):
    """Kiểm tra link stream sâu 2 tầng bằng Asynchronous"""
    # Dùng Semaphore để giới hạn số lượng request chạy cùng lúc (tránh nghẽn mạng hoặc bị ban IP)
    async with sem:
        try:
            # Mặt nạ VLC
            headers = {
                'User-Agent': 'VLC/3.0.16 LibVLC/3.0.16',
                'Accept': 'application/x-mpegURL, application/vnd.apple.mpegurl, */*',
                'Connection': 'keep-alive'
            }
            timeout = aiohttp.ClientTimeout(total=TIMEOUT)
            
            async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as response:
                # Vòng 1: Bắt lỗi HTTP lớp vỏ
                if response.status >= 400:
                    return url, False
                    
                # Vòng 2: Bắt lỗi Content-Type
                content_type = response.headers.get('Content-Type', '').lower()
                if 'html' in content_type or 'xml' in content_type:
                    return url, False
                    
                # Vòng 3: Tải 2048 bytes đầu tiên (aiohttp tự động giải nén GZIP)
                raw_data = await response.content.read(2048)
                text_data = raw_data.decode('utf-8', errors='ignore').strip()
                
                # Vòng 4: Kiểm tra M3U8 chuẩn & Bắt List Ma (File rỗng)
                # Đã thêm điều kiện: '/m3u8' hoặc file bắt đầu bằng #EXTM3U là túm cổ vào quét hết
                if '.m3u8' in url or '/m3u8' in url or 'mpegurl' in content_type or text_data.startswith('#EXTM3U'):
                    if not text_data.startswith('#EXTM3U'):
                        return url, False
                    
                    # [BẢN VÁ LIST MA] Bắt buộc file phải chứa thông số video (#EXTINF hoặc luồng)
                    if '#EXTINF' not in text_data and '#EXT-X-STREAM-INF' not in text_data:
                        return url, False
                    
                # Vòng 5: Bắt Soft 404
                text_data_lower = text_data.lower()
                error_keywords = ['404', 'not found', 'file not found', 'access denied', 'forbidden', 'banned', 'blocked', 'invalid token', 'error']
                for kw in error_keywords:
                    if kw in text_data_lower[:200]: 
                        return url, False

                # Vòng 6: Quét link con bên trong
                if '#EXTM3U' in text_data:
                    lines = text_data.splitlines()
                    inner_url = None
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            # aiohttp trả về response.url là đối tượng yarl.URL, cần ép kiểu str()
                            inner_url = urljoin(str(response.url), line)
                            break
                    
                    if inner_url:
                        try:
                            # Ping thử link con
                            async with session.get(inner_url, headers=headers, timeout=timeout, allow_redirects=True) as inner_resp:
                                if inner_resp.status >= 400:
                                    return url, False
                        except Exception:
                            return url, False

                return url, True
                
        except Exception:
            # Bắt mọi lỗi Timeout, Disconnect, DNS...
            return url, False


async def main():
    m3u_url = os.getenv('TV_M3U_SOURCE_URL')
    
    if not m3u_url:
        print("CẢNH BÁO: Chưa set TV_M3U_SOURCE_URL, sử dụng link mặc định để test.")
        m3u_url = "https://iptv-org.github.io/iptv/categories/sports.m3u"
        
    print(f"Đang tải playlist từ: {m3u_url}...")
    
    # 1. TẢI FILE M3U GỐC BẰNG AIOHTTP
    fetch_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    
    m3u_text = ""
    # Cấu hình timeout cho tải file tổng
    timeout = aiohttp.ClientTimeout(total=30)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(m3u_url, headers=fetch_headers, timeout=timeout) as response:
                response.raise_for_status() # Sinh lỗi nếu tải thất bại
                m3u_text = await response.text()
    except Exception as e:
        print(f"❌ Lỗi tải playlist gốc: {e}")
        sys.exit(1)

    # 2. PHÂN TÍCH VÀ LỌC TRÙNG LẶP
    lines = m3u_text.splitlines()
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

    unique_channels = []
    seen_urls = set()
    for extinf, url in channels:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_channels.append((extinf, url))
            
    print(f"Đã lọc bỏ {len(channels) - len(unique_channels)} kênh trùng lặp.")
    print(f"Bắt đầu kiểm tra siêu tốc {len(unique_channels)} kênh bằng Async...\n")

    # 3. KIỂM TRA ĐA LUỒNG ASYNC
    urls = [c[1] for c in unique_channels]
    check_results = {}
    
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT) # Tối ưu hóa pool kết nối
    
    async with aiohttp.ClientSession(connector=connector) as session:
        # Tạo danh sách các task
        tasks = [check_channel(sem, session, url) for url in urls]
        
        # as_completed giúp in kết quả ngay khi một kênh quét xong (không cần chờ tất cả)
        for coro in asyncio.as_completed(tasks):
            url, is_alive = await coro
            check_results[url] = is_alive
            if is_alive:
                print(f"[🟢 SỐNG] {url}")

    # 4. LƯU KẾT QUẢ VÀ TẠO HEADER
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
        header
    ]
    
    for extinf, url in alive_channels_list:
        if extinf:
            valid_m3u_lines.append(extinf)
        valid_m3u_lines.append(url)

    final_m3u_content = "\n".join(valid_m3u_lines) + "\n"

    output_path = os.path.join(os.getcwd(), OUTPUT_FILENAME)
    print(f"\nĐang ghi ra file: {output_path}")
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(final_m3u_content)
        
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("File bị rỗng hoặc không tạo được!")
            
        print(f"✅ Đã cập nhật thành công {OUTPUT_FILENAME}")
        print(f"📊 Kênh SỐNG/TỔNG: {alive_count}/{len(unique_channels)}")
        print(f"📦 Dung lượng file: {os.path.getsize(output_path)} bytes")
        
    except Exception as e:
        print(f"❌ Lỗi ghi file {output_path}: {str(e)}")
        if os.path.exists(output_path):
            os.remove(output_path)
        sys.exit(1)

if __name__ == "__main__":
    # Dùng asyncio.run để khởi động hàm main async
    # Windows đôi khi gặp lỗi EventLoop nếu không cấu hình chuẩn, dùng set_event_loop_policy để khắc phục
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Đã dừng đột ngột bởi người dùng!")
        sys.exit(1)
