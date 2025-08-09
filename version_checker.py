"""版本檢查客戶端模組"""
import os
import sys
import json
import uuid
import platform
import requests
import threading
from typing import Optional, Dict, Any

# 配置
CURRENT_VERSION = "1.0.0"
PROJECT_ID = "bg-flex-manager"
STATS_API_URL = "https://multi-project-stats.terrydev-tw.workers.dev"

# 配置檔案放到系統應用數據目錄（用戶看不到）
def get_config_file_path():
    """獲取配置檔案路徑（AppData 目錄）"""
    if os.name == 'nt':  # Windows
        app_data = os.environ.get('LOCALAPPDATA', os.path.expanduser('~\\AppData\\Local'))
        config_dir = os.path.join(app_data, 'BitgetFlexManagerCLI')
    else:  # macOS/Linux
        config_dir = os.path.expanduser('~/.bitget_flex_manager_cli')
    
    # 確保目錄存在
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, 'version_config.json')

CONFIG_FILE = get_config_file_path()

class VersionChecker:
    """版本檢查器"""
    
    def __init__(self):
        self.install_id = self._get_or_create_install_id()
        self.current_version = CURRENT_VERSION
        self.project_id = PROJECT_ID
        
    def _get_or_create_install_id(self) -> str:
        """獲取或創建安裝ID"""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    if 'install_id' in config:
                        return config['install_id']
        except Exception:
            pass
        
        # 創建新的 install_id
        install_id = str(uuid.uuid4())
        self._save_config({'install_id': install_id})
        return install_id
    
    def _save_config(self, config: Dict[str, Any]):
        """保存配置到文件"""
        try:
            existing_config = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    existing_config = json.load(f)
            
            existing_config.update(config)
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(existing_config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[警告] 無法保存配置: {e}")
    
    def _get_system_info(self) -> Dict[str, str]:
        """獲取系統資訊"""
        try:
            return {
                "os_info": f"{platform.system()} {platform.release()}",
                "arch": platform.machine(),
                "user_agent": f"BGFlexManager/{self.current_version}"
            }
        except Exception:
            return {
                "os_info": "Unknown",
                "arch": "Unknown", 
                "user_agent": f"BGFlexManager/{self.current_version}"
            }
    
    def check_version_sync(self) -> Optional[Dict[str, Any]]:
        """同步檢查版本更新"""
        try:
            system_info = self._get_system_info()
            
            payload = {
                "install_id": self.install_id,
                "version": self.current_version,
                **system_info
            }
            
            response = requests.post(
                f"{STATS_API_URL}/api/version-check/{self.project_id}",
                json=payload,
                timeout=10,
                headers={"User-Agent": system_info["user_agent"]}
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"[警告] 版本檢查失敗: HTTP {response.status_code}")
                return None
                
        except requests.RequestException as e:
            print(f"[警告] 版本檢查網絡錯誤: {e}")
            return None
        except Exception as e:
            print(f"[警告] 版本檢查意外錯誤: {e}")
            return None
    
    def check_version_async(self, callback=None):
        """異步檢查版本更新"""
        def worker():
            result = self.check_version_sync()
            if callback and result:
                callback(result)
        
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread
    
    def show_update_notification(self, version_info: Dict[str, Any]):
        """顯示更新通知"""
        if not version_info.get('update_available'):
            return
        
        latest_version = version_info.get('latest_version')
        download_url = version_info.get('download_url')
        
        print(f"\\n{'='*50}")
        print("📦 發現新版本！")
        print(f"當前版本: {self.current_version}")
        print(f"最新版本: {latest_version}")
        print(f"更新說明: {version_info.get('changelog', '版本更新')}")
        
        if download_url:
            print(f"下載連結: {download_url}")
        
        print(f"{'='*50}\\n")

def check_for_updates():
    """便捷函數：檢查更新並顯示通知"""
    checker = VersionChecker()
    
    def handle_result(result):
        checker.show_update_notification(result)
    
    # 異步檢查，不阻塞主程序
    checker.check_version_async(callback=handle_result)

def check_for_updates_blocking() -> bool:
    """便捷函數：阻塞式檢查更新"""
    checker = VersionChecker()
    result = checker.check_version_sync()
    
    if result:
        checker.show_update_notification(result)
        return result.get('update_available', False)
    
    return False

if __name__ == "__main__":
    # 測試版本檢查
    print("測試版本檢查...")
    has_update = check_for_updates_blocking()
    print(f"有更新可用: {has_update}")