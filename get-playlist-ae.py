import requests
import os
import aiohttp

# Cấu hình
URL = "http://77.137.40.221:8000/playlist.m3u8"
OUTPUT_FILE = "playlist_ae.m3u"

def download_playlist():
    print(f"Đang tải playlist từ: {URL}")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(URL, headers=headers, timeout=30)
        response.raise_for_status() # Kiểm tra lỗi HTTP
        
        # Đảm bảo nội dung có mã hóa đúng
        content = response.text
        
        # Lưu file
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(content)
            
        print(f"Đã lưu playlist thành công vào file: {OUTPUT_FILE}")
        return True
    except Exception as e:
        print(f"Lỗi khi tải playlist: {e}")
        return False

if __name__ == "__main__":
    if download_playlist():
        print("Hoàn thành công việc!")
    else:
        exit(1) # Báo lỗi cho GitHub Action nếu tải thất bại
