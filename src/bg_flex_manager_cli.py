#!/usr/bin/env python3
import random
import string
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from bitget_api import (
    get_virtual_subaccount_list, create_virtual_subaccount_batch, create_subaccount_apikey, 
    load_config, save_config, get_savings_products, get_savings_assets, get_spot_assets, 
    savings_subscribe, get_savings_subscribe_info, savings_redeem, transfer_to_subaccount,
    transfer_to_main_account, get_all_subaccount_assets, get_account_info
)
from version_checker import check_for_updates

# 配置參數
TARGET_SUBACCOUNT_COUNT = 4


def generate_subaccount_name():
    """生成8位純英文字母的子帳戶名稱"""
    return ''.join(random.choices(string.ascii_lowercase, k=8))


def get_my_ip():
    """獲取當前外網IP"""
    try:
        response = requests.get('https://api.ipify.org', timeout=10)
        return response.text.strip()
    except Exception as e:
        print(f"[錯誤] 無法獲取IP: {e}")
        return None


def generate_api_passphrase():
    """生成API Key密碼(8-32位英文字母+數字)"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=16))


def create_apikeys_for_subaccounts():
    """為所有子帳戶創建API Key"""
    print("\n=== 創建子帳戶API Key ===")
    
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    # 先檢查是否需要創建API Key
    subaccounts_to_create = []
    for account_id, account_info in config.get('accounts', {}).items():
        if account_info.get('type') == 'sub':
            apikey = account_info.get('apikey', '').strip()
            if not apikey or apikey == '':
                subaccounts_to_create.append({
                    'account_id': account_id,
                    'uuid': account_info['uuid']
                })
    
    if not subaccounts_to_create:
        print("[信息] 所有子帳戶都已有API Key，跳過創建")
        return True
    
    # 如果需要創建API Key，才詢問是否綁定IP
    bind_ip = input("是否綁定當前IP?(建議綁定) (Y/n): ").strip().lower()
    bind_ip = bind_ip != 'n'  # 預設是綁定
    
    ip_list = []
    if bind_ip:
        print("[信息] 正在獲取當前IP...")
        current_ip = get_my_ip()
        if current_ip:
            ip_list = [current_ip]
            print(f"[信息] 當前IP: {current_ip}")
        else:
            print("[警告] 無法獲取IP，將不綁定IP")
    
    print(f"[信息] 需要為 {len(subaccounts_to_create)} 個子帳戶創建API Key")
    
    success_count = 0
    for i, sub_info in enumerate(subaccounts_to_create):
        account_id = sub_info['account_id']
        sub_account_uid = sub_info['uuid']
        
        print(f"\n[步驟{i+1}] 為子帳戶 {account_id} (UID: {sub_account_uid}) 創建API Key...")
        
        # 生成密碼和標籤
        passphrase = generate_api_passphrase()
        label = f"auto_sub{account_id}"
        
        # 創建API Key
        result = create_subaccount_apikey(
            sub_account_uid=sub_account_uid,
            passphrase=passphrase,
            label=label,
            permissions=["transfer", "read", "spot_trade"],
            ip_list=ip_list
        )
        
        if result.get('code') == '00000':
            data = result.get('data', {})
            api_key = data.get('subAccountApiKey')
            secret_key = data.get('secretKey')
            
            # 更新配置文件
            config['accounts'][account_id].update({
                'apikey': api_key,
                'secret': secret_key,
                'passphrase': passphrase
            })
            
            print(f"[成功] API Key 創建成功")
            print(f"       API Key: {api_key}")
            print(f"       權限: {data.get('permList', [])}")
            if ip_list:
                print(f"       綁定IP: {data.get('ipList', [])}")
            
            success_count += 1
        else:
            print(f"[失敗] 創建失敗: {result}")
        
        # 限速保護：每次調用後休息0.5秒
        if i < len(subaccounts_to_create) - 1:  # 最後一次不需要等待
            time.sleep(0.5)
    
    # 保存更新後的配置
    save_config(config)
    print(f"\n[完成] 成功為 {success_count}/{len(subaccounts_to_create)} 個子帳戶創建API Key")
    
    return success_count > 0


def ensure_target_subaccounts():
    """確保有指定數量的虛擬子帳戶，不足則創建"""
    print("=== Bitget Flex CLI ===")
    
    # 步驟1: 獲取現有虛擬子帳戶列表
    print("\n[步驟1] 檢查現有虛擬子帳戶...")
    result = get_virtual_subaccount_list()
    
    if result.get('code') != '00000':
        print(f"[錯誤] {result}")
        return False
    
    data = result.get('data', {})
    existing_subaccounts = data.get('subAccountList', [])
    existing_count = len(existing_subaccounts)
    
    print(f"[信息] 目前有 {existing_count} 個虛擬子帳戶")
    
    # 步驟2: 如果不足目標數量，創建缺少的子帳戶
    if existing_count < TARGET_SUBACCOUNT_COUNT:
        needed_count = TARGET_SUBACCOUNT_COUNT - existing_count
        print(f"\n[步驟2] 需要創建 {needed_count} 個子帳戶...")
        
        # 生成要創建的子帳戶名稱列表
        new_subaccounts = []
        for i in range(needed_count):
            sub_name = generate_subaccount_name()
            new_subaccounts.append(sub_name)
            print(f"  [準備] 將創建: {sub_name}")
        
        # 批量創建子帳戶
        create_result = create_virtual_subaccount_batch(new_subaccounts)
        
        if create_result.get('code') == '00000':
            success_list = create_result.get('data', {}).get('successList', [])
            failure_list = create_result.get('data', {}).get('failureList', [])
            
            print(f"\n[結果] 成功創建 {len(success_list)} 個，失敗 {len(failure_list)} 個")
            
            if failure_list:
                print("[失敗列表]:")
                for fail in failure_list:
                    print(f"  - {fail.get('subaAccountName')}: {fail.get('reason', '未知原因')}")
        else:
            print(f"[錯誤] 批量創建失敗: {create_result}")
            return False
        
        # 重新獲取最新的子帳戶列表
        print("\n[步驟3] 重新獲取子帳戶列表...")
        result = get_virtual_subaccount_list()
        if result.get('code') != '00000':
            return False
        
        data = result.get('data', {})
        existing_subaccounts = data.get('subAccountList', [])
    
    # 檢查是否需要更新配置文件
    print(f"\n[步驟4] 檢查配置文件...")
    needs_update = check_config_needs_update(existing_subaccounts)
    
    if needs_update:
        print("[信息] 配置需要更新...")
        return update_config_with_subaccounts(existing_subaccounts)
    else:
        print("[信息] 配置已是最新，跳過更新")
        return True


def check_config_needs_update(subaccounts):
    """檢查配置文件是否需要更新"""
    config = load_config()
    if not config:
        return True  # 無配置文件，需要更新
    
    # 按 UUID 從小到大排序
    subaccounts_sorted = sorted(subaccounts[:TARGET_SUBACCOUNT_COUNT], key=lambda x: int(x.get('subAccountUid')))
    
    # 檢查數量是否一致
    existing_sub_accounts = {k: v for k, v in config.get('accounts', {}).items() 
                            if v.get('type') == 'sub'}
    
    if len(existing_sub_accounts) != len(subaccounts_sorted):
        return True  # 數量不一致，需要更新
    
    # 檢查UUID順序是否一致
    for i, sub in enumerate(subaccounts_sorted):
        account_id = str(i + 1)
        uid = sub.get('subAccountUid')
        
        if (account_id not in existing_sub_accounts or 
            existing_sub_accounts[account_id].get('uuid') != uid):
            return True  # UUID順序不一致，需要更新
    
    return False  # 配置已是最新


def update_config_with_subaccounts(subaccounts):
    """將子帳戶信息更新到配置文件"""
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    # 建立現有 UUID 到配置的映射，保留 API Key 信息
    existing_uuid_to_config = {}
    for account_id, account_info in config.get('accounts', {}).items():
        if account_info.get('type') == 'sub' and account_info.get('uuid'):
            existing_uuid_to_config[account_info['uuid']] = {
                'apikey': account_info.get('apikey', ''),
                'secret': account_info.get('secret', ''),
                'passphrase': account_info.get('passphrase', '')
            }
    
    # 按 UUID 從小到大排序，然後對應到 1-N
    subaccounts_sorted = sorted(subaccounts[:TARGET_SUBACCOUNT_COUNT], key=lambda x: int(x.get('subAccountUid')))
    
    for i, sub in enumerate(subaccounts_sorted):
        account_id = str(i + 1)  # 1, 2, 3, ..., N
        uid = sub.get('subAccountUid')
        
        # 如果是現有的 UUID，保留原有的 API Key 配置
        if uid in existing_uuid_to_config:
            existing_config = existing_uuid_to_config[uid]
            config['accounts'][account_id] = {
                "type": "sub",
                "uuid": uid,
                "apikey": existing_config['apikey'],
                "secret": existing_config['secret'], 
                "passphrase": existing_config['passphrase']
            }
            print(f"  [記錄] {account_id}: {uid} (保留現有API Key)")
        else:
            # 新的子帳戶，設定空的 API Key 配置
            config['accounts'][account_id] = {
                "type": "sub",
                "uuid": uid,
                "apikey": "",
                "secret": "", 
                "passphrase": ""
            }
            print(f"  [記錄] {account_id}: {uid} (新子帳戶)")
    
    # 保存更新後的配置
    save_config(config)
    print(f"\n[完成] 已記錄 {len(subaccounts_sorted)} 個子帳戶到配置文件")
    
    return True


def savings_management_workflow():
    """理財寶管理主流程"""
    print("\n=== 理財寶管理工具 ===")
    
    # 步驟0: 讓用戶輸入選擇幣種
    coin = input("請輸入要管理的幣種 (例如: USDT, BTC, ETH): ").strip().upper()
    if not coin:
        print("[錯誤] 幣種不能為空")
        return False
        
    print(f"[信息] 選擇的幣種: {coin}")
    print("=" * 50)
    
    # 步驟1: 查詢理財寶產品列表並讓用戶選擇
    selected_product = step1_query_savings_products(coin)
    if not selected_product:
        return False
    
    # 步驟2: 查詢每個帳戶理財寶資產狀況
    account_status = step2_query_current_assets(coin, selected_product)
    if not account_status:
        return False
    
    # 步驟3: 用戶選擇申購策略
    operations = step3_user_selection(coin, selected_product, account_status)
    if operations is None:  # 用戶取消或錯誤
        return False
    
    # 步驟4: 執行申購操作
    if not step4_execute_operations(coin, selected_product, operations):
        return False
    
    # 步驟5: 再次查詢並顯示最終狀況
    if not step5_final_query(coin, selected_product, account_status):
        return False
    
    print("\n" + "=" * 50)
    print("[完成] 理財寶管理流程執行完成")
    return True


def step1_query_savings_products(coin):
    """步驟1: 查詢理財寶產品列表"""
    print(f"\n=== 步驟1: 查詢 {coin} 理財寶產品列表 ===")
    
    # 使用主帳戶查詢產品列表（所有帳戶看到的產品都一樣）
    result = get_savings_products(coin=coin, filter_type='available', account_key='main')
    
    if result.get('code') != '00000':
        print(f"[錯誤] 查詢產品失敗: {result}")
        return False
    
    products = result.get('data', [])
    if not products:
        print(f"[錯誤] 沒有找到 {coin} 的可申購理財寶產品")
        return False
    
    print(f"[信息] 找到 {len(products)} 個可申購產品:")
    
    # 顯示所有產品信息
    for i, product in enumerate(products):
        product_id = product.get('productId')
        period_type = product.get('periodType')
        period = product.get('period', '')
        apy_type = product.get('apyType')
        apy_list = product.get('apyList', [])
        product_level = product.get('productLevel', 'normal')
        
        period_text = "活期" if period_type == 'flexible' else f"{period}天定期"
        level_text = f" ({product_level})" if product_level != 'normal' else ""
        
        print(f"  [{i+1}] {period_text}產品{level_text}")
        print(f"      產品ID: {product_id}")
        print(f"      利率類型: {apy_type}")
        
        # 顯示利率階梯
        for j, apy in enumerate(apy_list):
            min_val = apy.get('minStepVal', '0')
            max_val = apy.get('maxStepVal', '0')
            current_apy = apy.get('currentApy', '0')
            
            if float(max_val) >= 120000000:  # 很大的數字表示無上限
                print(f"      - 階梯{j+1}: {min_val}+ {coin} → {current_apy}% 年化")
            else:
                print(f"      - 階梯{j+1}: {min_val}-{max_val} {coin} → {current_apy}% 年化")
        
        print()
    
    # 讓用戶選擇產品
    while True:
        try:
            choice = input(f"請選擇產品 (1-{len(products)}): ").strip()
            choice_num = int(choice)
            if 1 <= choice_num <= len(products):
                selected_product = products[choice_num - 1]
                break
            else:
                print(f"[錯誤] 請輸入 1-{len(products)} 之間的數字")
        except ValueError:
            print("[錯誤] 請輸入有效的數字")
        except KeyboardInterrupt:
            print("\n[取消] 用戶取消操作")
            return False
    
    product_type = "活期" if selected_product.get('periodType') == 'flexible' else f"{selected_product.get('period')}天定期"
    print(f"[選擇] 已選擇 {product_type}產品 (ID: {selected_product.get('productId')})")
    
    return selected_product


def step2_query_current_assets(coin, selected_product):
    """步驟2: 查詢每個帳戶的理財寶資產狀況"""
    print(f"\n=== 步驟2: 查詢所有帳戶理財寶資產狀況 ===")
    
    product_id = selected_product.get('productId')
    period_type = selected_product.get('periodType')
    product_name = "活期" if period_type == 'flexible' else f"{selected_product.get('period')}天定期"
    
    print(f"[產品信息] {product_name}產品 (ID: {product_id})")
    
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    accounts = {}
    for account_id, account_info in config.get('accounts', {}).items():
        if account_info.get('type') in ['main', 'sub']:
            # 檢查是否有有效的API Key
            if account_info.get('apikey') and account_info.get('secret'):
                accounts[account_id] = account_info
    
    if not accounts:
        print("[錯誤] 沒有找到有效的帳戶配置")
        return False
    
    print(f"[信息] 正在查詢 {len(accounts)} 個帳戶的狀況...")
    
    account_status = {}
    
    for account_id in accounts:
        # print(f"\n[查詢] 帳戶 {account_id} ({accounts[account_id].get('type')})...")
        
        # 使用ThreadPoolExecutor並行執行3個API查詢
        with ThreadPoolExecutor(max_workers=3) as executor:
            # 同時提交3個API查詢任務
            future_subscribe = executor.submit(get_savings_subscribe_info, product_id, period_type, account_id)
            future_savings = executor.submit(get_savings_assets, period_type, 20, account_id)
            future_wallet = executor.submit(get_spot_assets, coin, account_id)
            
            # 獲取結果
            subscribe_info = future_subscribe.result()
            savings_result = future_savings.result()
            wallet_result = future_wallet.result()
        
        account_status[account_id] = {
            'account_info': accounts[account_id],
            'subscribe_info': subscribe_info,
            'savings_result': savings_result,
            'wallet_result': wallet_result
        }
        
        # 顯示基本狀態
        subscribe_success = subscribe_info.get('code') == '00000'
        savings_success = savings_result.get('code') == '00000'
        wallet_success = wallet_result.get('code') == '00000'
        
        # 顯示詳細信息 - 查找該產品的持有量
        personal_holding = 0
        if savings_success:
            savings_data = savings_result.get('data', {})
            result_list = savings_data.get('resultList', [])
            
            # 查找該產品ID的持有量
            for item in result_list:
                if item.get('productId') == product_id:
                    personal_holding = float(item.get('holdAmount', '0'))
                    break
            
            #print(f"  - 個人持有: {personal_holding:.6f} {coin}")
        
        if wallet_success and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                available = float(wallet_data[0].get('available', '0'))
                #print(f"  - 錢包可用: {available:.6f} {coin}")
        
    
    print(f"\n[信息] 資產查詢完成")
    
    # TODO: 格式化顯示總覽和分析
    
    return account_status


def step3_user_selection(coin, selected_product, account_status):
    """步驟3: 用戶選擇申購策略"""
    print(f"\n=== 步驟3: 選擇申購策略 ===")
    
    # 解析產品階梯信息
    apy_list = selected_product.get('apyList', [])
    if not apy_list:
        print("[錯誤] 無法獲取產品階梯信息")
        return None
    
    # 顯示階梯信息
    print(f"\n[產品階梯信息]")
    tier1_limit = 0
    
    for i, apy in enumerate(apy_list):
        min_val = apy.get('minStepVal', '0')
        max_val = apy.get('maxStepVal', '0')
        current_apy = apy.get('currentApy', '0')
        if i == 0:  # 第一階梯
            tier1_limit = float(max_val)
        if float(max_val) >= 120000000:
            print(f"  階梯{i+1}: {min_val}+ {coin} → {current_apy}% 年化")
        else:
            print(f"  階梯{i+1}: {min_val}-{max_val} {coin} → {current_apy}% 年化")
    
    # 顯示當前帳戶狀況總覽
    print(f"\n[帳戶狀況總覽]")
    valid_accounts = []
    
    for account_id, status in account_status.items():
        account_type = status['account_info'].get('type')
        
        # 獲取個人持有量
        personal_holding = 0
        savings_result = status.get('savings_result', {})
        if savings_result.get('code') == '00000':
            result_list = savings_result.get('data', {}).get('resultList', [])
            for item in result_list:
                if item.get('productId') == selected_product.get('productId'):
                    personal_holding = float(item.get('holdAmount', '0'))
                    break
        
        # 獲取錢包餘額
        wallet_available = 0
        wallet_result = status.get('wallet_result', {})
        if wallet_result.get('code') == '00000' and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                wallet_available = float(wallet_data[0].get('available', '0'))
        
        # 計算到第一階梯上限的空間
        space_to_tier1 = max(0, tier1_limit - personal_holding)
        
        account_name = f"{'主帳戶' if account_type == 'main' else f'子帳戶{account_id}'}"
        print(f"  {account_name}: 持有={personal_holding}, 錢包={wallet_available}, 到{tier1_limit}還可存={space_to_tier1}")
        
        valid_accounts.append({
            'id': account_id,
            'name': account_name,
            'type': account_type,
            'holding': personal_holding,
            'wallet': wallet_available,
            'space_to_tier1': space_to_tier1
        })
    
    # 用戶選擇帳戶
    print(f"\n[選擇要操作的帳戶]")
    print("0. 全部帳戶")
    for i, acc in enumerate(valid_accounts):
        print(f"{i+1}. {acc['name']}")
    
    try:
        choice = input("請選擇 (0-{0}): ".format(len(valid_accounts))).strip()
        choice_num = int(choice)
        
        if choice_num == 0:
            selected_accounts = valid_accounts
            print("[選擇] 全部帳戶")
        elif 1 <= choice_num <= len(valid_accounts):
            selected_accounts = [valid_accounts[choice_num - 1]]
            print(f"[選擇] {valid_accounts[choice_num - 1]['name']}")
        else:
            print("[錯誤] 無效選擇")
            return None
    except (ValueError, KeyboardInterrupt):
        print("[取消] 用戶取消操作")
        return None
    
    # 用戶選擇操作類型
    print(f"\n[選擇操作類型]")
    print(f"1. 存入到填滿{tier1_limit} (第一階梯上限)")
    print(f"2. 取出到剩{tier1_limit} (保留第一階梯上限)")
    print("3. 全部取出")
    
    try:
        op_choice = input("請選擇操作 (1-3): ").strip()
        if op_choice not in ['1', '2', '3']:
            print("[錯誤] 無效選擇")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    # 計算操作計劃
    operations = []
    for account in selected_accounts:
        if op_choice == '1':  # 存入到填滿第一階梯上限
            can_deposit = min(account['wallet'], account['space_to_tier1'])
            if can_deposit >= 0.1:  # 最小申購金額
                operations.append({
                    'account_id': account['id'],
                    'account_name': account['name'],
                    'action': 'subscribe',
                    'amount': can_deposit,
                    'reason': f"申購 {can_deposit:.6f} (錢包可用: {account['wallet']:.6f})"
                })
            else:
                print(f"  跳過 {account['name']}: 錢包餘額不足最小申購金額0.1 (當前: {account['wallet']:.6f})")
        elif op_choice == '2':  # 取出到剩300
            if account['holding'] > tier1_limit:
                redeem_amount = account['holding'] - tier1_limit
                operations.append({
                    'account_id': account['id'],
                    'account_name': account['name'],
                    'action': 'redeem',
                    'amount': redeem_amount
                })
        elif op_choice == '3':  # 全部取出
            if account['holding'] > 0:
                operations.append({
                    'account_id': account['id'],
                    'account_name': account['name'],
                    'action': 'redeem',
                    'amount': account['holding']
                })
    
    # 顯示操作計劃
    if not operations:
        print("\n[信息] 沒有需要執行的操作")
        print("當前帳戶狀態已符合選擇的策略目標")
        return []
    
    print(f"\n[操作計劃]")
    for op in operations:
        action_text = "申購" if op['action'] == 'subscribe' else "贖回"
        print(f"  {op['account_name']}: {action_text} {op['amount']:.6f} {coin}")
    
    # 確認執行
    try:
        confirm = input(f"\n確認執行以上操作? (y/N): ").strip().lower()
        if confirm != 'y':
            print("[取消] 用戶取消操作")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    return operations


def step4_execute_operations(coin, selected_product, operations):
    """步驟4: 執行申購/贖回操作"""
    print(f"\n=== 步驟4: 執行操作 ===")
    
    if not operations:
        print("[信息] 沒有操作需要執行")
        return True
    
    product_id = selected_product.get('productId')
    period_type = selected_product.get('periodType')
    
    success_count = 0
    total_count = len(operations)
    
    print(f"[信息] 開始執行 {total_count} 個操作...")
    
    for i, op in enumerate(operations):
        account_id = op['account_id']
        account_name = op['account_name']
        action = op['action']
        amount = op['amount']
        
        print(f"\n[執行 {i+1}/{total_count}] {account_name} - ", end="")
        
        try:
            if action == 'subscribe':
                print(f"申購 {amount:.6f} {coin}")
                result = savings_subscribe(product_id, period_type, amount, account_key=account_id)
            elif action == 'redeem':
                print(f"贖回 {amount:.6f} {coin}")
                result = savings_redeem(product_id, period_type, amount, account_key=account_id)
            else:
                print(f"[錯誤] 未知操作類型: {action}")
                continue
            
            # 檢查執行結果
            if result.get('code') == '00000':
                order_id = result.get('data', {}).get('orderId', '')
                print(f"  ✅ 成功 (訂單ID: {order_id})")
                success_count += 1
            else:
                error_msg = result.get('msg', '未知錯誤')
                print(f"  ❌ 失敗: {error_msg}")
                print(f"     詳細: {result}")
            
            # 輕微延遲避免過於頻繁請求
            if i < total_count - 1:  # 最後一次不需要等待
                time.sleep(0.2)
                
        except Exception as e:
            print(f"  ❌ 異常: {e}")
    
    print(f"\n[完成] 操作執行完成")
    print(f"  成功: {success_count}/{total_count}")
    print(f"  失敗: {total_count - success_count}/{total_count}")
    
    
    return True


def step5_final_query(coin, selected_product, original_account_status):
    """步驟5: 再次查詢並顯示最終狀況"""
    print(f"\n=== 步驟5: 最終狀況查詢 ===")
    
    product_id = selected_product.get('productId')
    period_type = selected_product.get('periodType')
    product_name = "活期" if period_type == 'flexible' else f"{selected_product.get('period')}天定期"
    
    print(f"[等待] 等待5秒讓申購操作結算...")
    time.sleep(5)
    
    print(f"[查詢] {product_name}產品最新狀況...")
    
    # 重新查詢所有帳戶狀況
    final_account_status = step2_query_current_assets(coin, selected_product)
    if not final_account_status:
        print("[錯誤] 無法查詢最終狀況")
        return False
    
    # 顯示前後對比
    print(f"\n=== 📊 執行結果對比 ===")
    
    # 解析產品階梯信息
    apy_list = selected_product.get('apyList', [])
    tier1_limit = float(apy_list[0].get('maxStepVal', '0')) if apy_list else 0
    
    print(f"產品: {product_name} (第一階梯上限: {tier1_limit} {coin})")
    print(f"{'帳戶':<8} {'執行前持有':<12} {'執行前錢包':<12} {'執行後持有':<12} {'執行後錢包':<12} {'變化':<20}")
    print("-" * 80)
    
    total_before_holding = 0
    total_after_holding = 0
    total_before_wallet = 0
    total_after_wallet = 0
    
    for account_id in final_account_status.keys():
        # 執行前數據
        before_data = original_account_status.get(account_id, {})
        before_holding = get_account_holding(before_data, product_id)
        before_wallet = get_account_wallet(before_data)
        
        # 執行後數據  
        after_data = final_account_status.get(account_id, {})
        after_holding = get_account_holding(after_data, product_id)
        after_wallet = get_account_wallet(after_data)
        
        # 計算變化
        holding_change = after_holding - before_holding
        wallet_change = after_wallet - before_wallet
        
        # 變化描述
        if abs(holding_change) < 0.000001:
            change_desc = "無變化"
        elif holding_change > 0:
            change_desc = f"申購 +{holding_change:.6f}"
        else:
            change_desc = f"贖回 {holding_change:.6f}"
        
        # 帳戶名稱
        account_type = after_data.get('account_info', {}).get('type', '')
        account_name = "主帳戶" if account_type == 'main' else f"子帳戶{account_id}"
        
        print(f"{account_name:<8} {before_holding:<12.6f} {before_wallet:<12.6f} {after_holding:<12.6f} {after_wallet:<12.6f} {change_desc:<20}")
        
        # 累計統計
        total_before_holding += before_holding
        total_after_holding += after_holding  
        total_before_wallet += before_wallet
        total_after_wallet += after_wallet
    
    # 顯示總計
    print("-" * 80)
    total_holding_change = total_after_holding - total_before_holding
    total_wallet_change = total_after_wallet - total_before_wallet
    
    if abs(total_holding_change) < 0.000001:
        total_change_desc = "無變化"
    elif total_holding_change > 0:
        total_change_desc = f"總申購 +{total_holding_change:.6f}"
    else:
        total_change_desc = f"總贖回 {total_holding_change:.6f}"
    
    print(f"{'總計':<8} {total_before_holding:<12.6f} {total_before_wallet:<12.6f} {total_after_holding:<12.6f} {total_after_wallet:<12.6f} {total_change_desc:<20}")
    
    # 階梯分析
    print(f"\n=== 📈 階梯分析 ===")
    tier1_accounts = 0
    tier2_accounts = 0
    
    for account_id in final_account_status.keys():
        after_data = final_account_status.get(account_id, {})
        after_holding = get_account_holding(after_data, product_id)
        account_type = after_data.get('account_info', {}).get('type', '')
        account_name = "主帳戶" if account_type == 'main' else f"子帳戶{account_id}"
        
        if after_holding > tier1_limit and len(apy_list) > 1:
            tier2_accounts += 1
            tier2_apy = apy_list[1].get('currentApy', '0')
            print(f"  {account_name}: {after_holding:.2f} {coin} (第二階梯 {tier2_apy}%)")
        elif after_holding > tier1_limit:
            # 只有一個階梯，但超過了上限（理論上不應該發生）
            tier1_accounts += 1
            tier1_apy = apy_list[0].get('currentApy', '0')
            print(f"  {account_name}: {after_holding:.2f} {coin} (超過第一階梯上限 {tier1_apy}%)")
        elif after_holding > 0:
            tier1_accounts += 1
            tier1_apy = apy_list[0].get('currentApy', '0')
            space_left = tier1_limit - after_holding
            print(f"  {account_name}: {after_holding:.2f} {coin} (第一階梯 {tier1_apy}%, 還可存{space_left:.2f})")
        else:
            print(f"  {account_name}: 0.00 {coin} (未投資)")
    
    if len(apy_list) > 1:
        print(f"\n第一階梯帳戶數: {tier1_accounts}, 第二階梯帳戶數: {tier2_accounts}")
    else:
        print(f"\n投資帳戶數: {tier1_accounts} (此產品只有單一利率階梯)")
    
    return True


def get_account_holding(account_data, product_id):
    """從帳戶數據中提取指定產品的持有量"""
    savings_result = account_data.get('savings_result', {})
    if savings_result.get('code') == '00000':
        result_list = savings_result.get('data', {}).get('resultList', [])
        for item in result_list:
            if item.get('productId') == product_id:
                return float(item.get('holdAmount', '0'))
    return 0.0


def get_account_wallet(account_data):
    """從帳戶數據中提取錢包餘額"""
    wallet_result = account_data.get('wallet_result', {})
    if wallet_result.get('code') == '00000' and wallet_result.get('data'):
        wallet_data = wallet_result.get('data', [])
        if wallet_data:
            return float(wallet_data[0].get('available', '0'))
    return 0.0


def transfer_management_workflow():
    """主子帳戶轉帳管理主流程"""
    print("\n=== 主子帳戶轉帳管理 ===")
    
    # 步驟0: 確保主帳戶UID已記錄
    print("\n[步驟0] 檢查主帳戶UID配置...")
    if not ensure_main_account_uid():
        print("[錯誤] 主帳戶UID配置失敗，無法進行轉帳操作")
        return False
    
    # 步驟1: 讓用戶輸入選擇幣種
    coin = input("\n請輸入要轉帳的幣種 (例如: USDT, BTC, ETH): ").strip().upper()
    if not coin:
        print("[錯誤] 幣種不能為空")
        return False
        
    print(f"[信息] 選擇的幣種: {coin}")
    print("=" * 50)
    
    # 步驟2: 查詢每個帳戶餘額並顯示
    account_balances = transfer_step1_query_balances(coin)
    if not account_balances:
        return False
    
    # 步驟3: 用戶選擇轉帳策略
    operations = transfer_step2_user_selection(coin, account_balances)
    if operations is None:  # 用戶取消或錯誤
        return False
    
    # 步驟4: 執行轉帳操作
    if not transfer_step3_execute_operations(coin, operations):
        return False
    
    # 步驟5: 再次查詢並顯示最終狀況
    if not transfer_step4_final_query(coin, account_balances):
        return False
    
    print("\n" + "=" * 50)
    print("[完成] 轉帳管理流程執行完成")
    return True


def transfer_step1_query_balances(coin):
    """步驟1: 查詢所有帳戶的指定幣種餘額 (使用與理財寶相同的邏輯確保一致性)"""
    print(f"\n=== 步驟1: 查詢所有帳戶 {coin} 餘額 ===")
    
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    accounts = {}
    for account_id, account_info in config.get('accounts', {}).items():
        if account_info.get('type') in ['main', 'sub']:
            # 檢查是否有有效的API Key
            if account_info.get('apikey') and account_info.get('secret'):
                accounts[account_id] = account_info
    
    if not accounts:
        print("[錯誤] 沒有找到有效的帳戶配置")
        return False
    
    print(f"[信息] 正在查詢 {len(accounts)} 個帳戶的 {coin} 餘額...")
    
    account_balances = {}
    
    # 逐個查詢每個帳戶 (與理財寶功能保持一致)
    for account_id in accounts:
        account_type = accounts[account_id].get('type')
        print(f"\n[查詢] 帳戶 {account_id} ({account_type})...")
        
        # 查詢現貨錢包餘額
        wallet_result = get_spot_assets(coin, account_id)
        
        account_balances[account_id] = {
            'account_info': accounts[account_id],
            'wallet_result': wallet_result
        }
        
        # 顯示查詢結果
        wallet_success = wallet_result.get('code') == '00000'
        if wallet_success and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                available = float(wallet_data[0].get('available', '0'))
                frozen = float(wallet_data[0].get('frozen', '0'))
                total = available + frozen
                print(f"  - 可用: {available:.6f} {coin}")
                print(f"  - 凍結: {frozen:.6f} {coin}")
                print(f"  - 總計: {total:.6f} {coin}")
            else:
                print(f"  - 無 {coin} 餘額")
        else:
            print(f"  - 查詢失敗: {wallet_result.get('msg', '未知錯誤')}")
    
    # 顯示總覽表格
    print(f"\n=== {coin} 餘額總覽 ===")
    print(f"{'帳戶':<12} {'類型':<6} {'可用餘額':<15} {'凍結餘額':<15} {'總餘額':<15}")
    print("-" * 70)
    
    total_available = 0
    total_frozen = 0
    
    for account_id, balance_info in account_balances.items():
        account_type = balance_info['account_info'].get('type')
        account_name = f"{'主帳戶' if account_type == 'main' else f'子帳戶{account_id}'}"
        
        wallet_result = balance_info.get('wallet_result', {})
        available = 0
        frozen = 0
        if wallet_result.get('code') == '00000' and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                available = float(wallet_data[0].get('available', '0'))
                frozen = float(wallet_data[0].get('frozen', '0'))
        
        total_balance = available + frozen
        print(f"{account_name:<12} {account_type:<6} {available:<15.6f} {frozen:<15.6f} {total_balance:<15.6f}")
        
        total_available += available
        total_frozen += frozen
    
    print("-" * 70)
    total_all = total_available + total_frozen
    print(f"{'總計':<12} {'--':<6} {total_available:<15.6f} {total_frozen:<15.6f} {total_all:<15.6f}")
    
    return account_balances


def transfer_step2_user_selection(coin, account_balances):
    """步驟2: 用戶選擇轉帳策略"""
    print(f"\n=== 步驟2: 選擇轉帳策略 ===")
    
    # 分析帳戶狀況
    main_balance = 0
    sub_accounts = []
    
    for account_id, balance_info in account_balances.items():
        account_type = balance_info['account_info'].get('type')
        
        wallet_result = balance_info.get('wallet_result', {})
        available = 0
        if wallet_result.get('code') == '00000' and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                available = float(wallet_data[0].get('available', '0'))
        
        if account_type == 'main':
            main_balance = available
        else:
            sub_accounts.append({
                'id': account_id,
                'name': f'子帳戶{account_id}',
                'uuid': balance_info['account_info'].get('uuid'),
                'balance': available
            })
    
    # 用戶選擇轉帳方向
    print(f"\n[轉帳方向選擇]")
    print("1. 主帳戶轉出到子帳戶")
    print("2. 子帳戶轉回主帳戶")
    
    try:
        direction_choice = input("請選擇轉帳方向 (1-2): ").strip()
        if direction_choice not in ['1', '2']:
            print("[錯誤] 無效選擇")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    operations = []
    
    if direction_choice == '1':  # 主轉子
        print(f"\n[主帳戶轉出] 主帳戶可用餘額: {main_balance:.6f} {coin}")
        
        if main_balance <= 0:
            print("[錯誤] 主帳戶餘額不足")
            return None
        
        # 輸入每個帳號的轉帳金額
        try:
            amount_input = input(f"請輸入每個帳號的轉帳金額: ").strip()
            transfer_amount_per_account = float(amount_input)
            if transfer_amount_per_account <= 0:
                print("[錯誤] 轉帳金額必須大於0")
                return None
        except (ValueError, KeyboardInterrupt):
            print("[錯誤] 金額格式錯誤或用戶取消")
            return None
        
        # 選擇目標子帳戶
        print(f"\n[目標選擇]")
        print("0. 所有子帳戶")
        for i, sub in enumerate(sub_accounts):
            print(f"{i+1}. {sub['name']} (當前餘額: {sub['balance']:.6f})")
        print(f"多選範例: 輸入 '1,2,3' 選擇多個帳戶")
        
        try:
            target_choice = input(f"請選擇目標 (0-{len(sub_accounts)} 或多選如 1,2,3): ").strip()
            
            # 解析選擇
            selected_subs = []
            
            if target_choice == '0':
                # 所有子帳戶
                selected_subs = sub_accounts
                print("[選擇] 所有子帳戶")
            elif ',' in target_choice:
                # 多選格式，如1,2,3
                try:
                    target_numbers = [int(x.strip()) for x in target_choice.split(',')]
                    for num in target_numbers:
                        if 1 <= num <= len(sub_accounts):
                            selected_subs.append(sub_accounts[num - 1])
                        else:
                            print(f"[錯誤] 無效選擇: {num}")
                            return None
                    sub_names = [sub['name'] for sub in selected_subs]
                    print(f"[選擇] {', '.join(sub_names)}")
                except ValueError:
                    print("[錯誤] 多選格式錯誤，請使用如 1,2,3 的格式")
                    return None
            else:
                # 單選
                target_num = int(target_choice)
                if 1 <= target_num <= len(sub_accounts):
                    selected_subs = [sub_accounts[target_num - 1]]
                    print(f"[選擇] {selected_subs[0]['name']}")
                else:
                    print("[錯誤] 無效選擇")
                    return None
            
            # 檢查總金額是否超出主帳戶餘額
            total_transfer_amount = transfer_amount_per_account * len(selected_subs)
            if total_transfer_amount > main_balance:
                print(f"[錯誤] 總轉帳金額 {total_transfer_amount:.6f} 超過主帳戶餘額 {main_balance:.6f}")
                
                # 計算在當前金額下最多可以轉幾個帳戶
                max_accounts = int(main_balance / transfer_amount_per_account)
                if max_accounts > 0:
                    remaining_balance = main_balance - (max_accounts * transfer_amount_per_account)
                    print(f"[建議] 以每個 {transfer_amount_per_account:.6f} {coin} 計算，最多可轉 {max_accounts} 個帳戶")
                    print(f"        這樣會用掉 {max_accounts * transfer_amount_per_account:.6f} {coin}，剩餘 {remaining_balance:.6f} {coin}")
                    
                    # 詢問用戶是否要選前N個帳戶
                    try:
                        auto_select = input(f"是否轉帳到前 {max_accounts} 個選中的帳戶? (y/N): ").strip().lower()
                        if auto_select == 'y':
                            # 自動選擇前N個帳戶
                            selected_subs = selected_subs[:max_accounts]
                            selected_names = [sub['name'] for sub in selected_subs]
                            print(f"[自動調整] 將轉帳到: {', '.join(selected_names)}")
                            print(f"[新計劃] 總轉帳金額: {max_accounts * transfer_amount_per_account:.6f} {coin}")
                        else:
                            print("[取消] 請重新輸入轉帳金額或選擇帳戶")
                            return None
                    except KeyboardInterrupt:
                        print("[取消] 用戶取消操作")
                        return None
                else:
                    print(f"[錯誤] 主帳戶餘額不足以轉帳 {transfer_amount_per_account:.6f} {coin} 到任何帳戶")
                    return None
            
            # 創建轉帳操作
            for sub in selected_subs:
                operations.append({
                    'type': 'main_to_sub',
                    'from_account': 'main',
                    'to_account': sub['id'],
                    'to_uuid': sub['uuid'],
                    'amount': transfer_amount_per_account,
                    'description': f"主帳戶 → {sub['name']}: {transfer_amount_per_account:.6f} {coin}"
                })
                
        except (ValueError, KeyboardInterrupt):
            print("[錯誤] 選擇無效或用戶取消")
            return None
            
    else:  # 子轉主
        print(f"\n[子帳戶轉回]")
        
        # 顯示有餘額的子帳戶
        subs_with_balance = [sub for sub in sub_accounts if sub['balance'] > 0]
        if not subs_with_balance:
            print("[錯誤] 沒有子帳戶有餘額")
            return None
        
        # 選擇轉回方式
        print("[轉回方式選擇]")
        print("1. 全部轉回（每個子帳戶的全部餘額）")
        print("2. 指定金額轉回（每個選中的子帳戶轉回相同金額）")
        
        try:
            transfer_mode = input("請選擇轉回方式 (1-2): ").strip()
            if transfer_mode not in ['1', '2']:
                print("[錯誤] 無效選擇")
                return None
        except KeyboardInterrupt:
            print("[取消] 用戶取消操作")
            return None
        
        # 根據轉回方式處理
        if transfer_mode == '1':
            # 全部轉回模式 - 選擇帳戶
            print(f"\n[帳戶選擇]")
            print("0. 所有有餘額的子帳戶")
            for i, sub in enumerate(subs_with_balance):
                print(f"{i+1}. {sub['name']} (餘額: {sub['balance']:.6f})")
            print(f"多選範例: 輸入 '1,2,3' 選擇多個帳戶")
            
            try:
                source_choice = input(f"請選擇來源 (0-{len(subs_with_balance)} 或多選如 1,2,3): ").strip()
                
                # 解析選擇
                selected_subs = []
                
                if source_choice == '0':
                    # 所有有餘額的子帳戶
                    selected_subs = subs_with_balance
                    print("[選擇] 所有有餘額的子帳戶")
                elif ',' in source_choice:
                    # 多選格式
                    try:
                        source_numbers = [int(x.strip()) for x in source_choice.split(',')]
                        for num in source_numbers:
                            if 1 <= num <= len(subs_with_balance):
                                selected_subs.append(subs_with_balance[num - 1])
                            else:
                                print(f"[錯誤] 無效選擇: {num}")
                                return None
                        sub_names = [sub['name'] for sub in selected_subs]
                        print(f"[選擇] {', '.join(sub_names)}")
                    except ValueError:
                        print("[錯誤] 多選格式錯誤，請使用如 1,2,3 的格式")
                        return None
                else:
                    # 單選
                    source_num = int(source_choice)
                    if 1 <= source_num <= len(subs_with_balance):
                        selected_subs = [subs_with_balance[source_num - 1]]
                        print(f"[選擇] {selected_subs[0]['name']}")
                    else:
                        print("[錯誤] 無效選擇")
                        return None
                
                # 全部轉回
                for sub in selected_subs:
                    operations.append({
                        'type': 'sub_to_main',
                        'from_account': sub['id'],
                        'from_uuid': sub['uuid'],
                        'to_account': 'main',
                        'amount': sub['balance'],
                        'description': f"{sub['name']} → 主帳戶: {sub['balance']:.6f} {coin} (全部餘額)"
                    })
                    
            except (ValueError, KeyboardInterrupt):
                print("[錯誤] 選擇無效或用戶取消")
                return None
        
        else:  # transfer_mode == '2'
            # 指定金額轉回模式
            try:
                amount_input = input(f"請輸入每個帳號的轉回金額: ").strip()
                transfer_amount_per_account = float(amount_input)
                if transfer_amount_per_account <= 0:
                    print("[錯誤] 轉回金額必須大於0")
                    return None
            except (ValueError, KeyboardInterrupt):
                print("[錯誤] 金額格式錯誤或用戶取消")
                return None
            
            # 篩選出餘額足夠的子帳戶
            eligible_subs = [sub for sub in subs_with_balance if sub['balance'] >= transfer_amount_per_account]
            if not eligible_subs:
                print(f"[錯誤] 沒有子帳戶的餘額 >= {transfer_amount_per_account:.6f}")
                return None
            
            print(f"\n[帳戶選擇] (餘額 >= {transfer_amount_per_account:.6f})")
            print("0. 所有符合條件的子帳戶")
            for i, sub in enumerate(eligible_subs):
                print(f"{i+1}. {sub['name']} (餘額: {sub['balance']:.6f})")
            print(f"多選範例: 輸入 '1,2,3' 選擇多個帳戶")
            
            try:
                source_choice = input(f"請選擇來源 (0-{len(eligible_subs)} 或多選如 1,2,3): ").strip()
                
                # 解析選擇
                selected_subs = []
                
                if source_choice == '0':
                    # 所有符合條件的子帳戶
                    selected_subs = eligible_subs
                    print("[選擇] 所有符合條件的子帳戶")
                elif ',' in source_choice:
                    # 多選格式
                    try:
                        source_numbers = [int(x.strip()) for x in source_choice.split(',')]
                        for num in source_numbers:
                            if 1 <= num <= len(eligible_subs):
                                selected_subs.append(eligible_subs[num - 1])
                            else:
                                print(f"[錯誤] 無效選擇: {num}")
                                return None
                        sub_names = [sub['name'] for sub in selected_subs]
                        print(f"[選擇] {', '.join(sub_names)}")
                    except ValueError:
                        print("[錯誤] 多選格式錯誤，請使用如 1,2,3 的格式")
                        return None
                else:
                    # 單選
                    source_num = int(source_choice)
                    if 1 <= source_num <= len(eligible_subs):
                        selected_subs = [eligible_subs[source_num - 1]]
                        print(f"[選擇] {selected_subs[0]['name']}")
                    else:
                        print("[錯誤] 無效選擇")
                        return None
                
                # 指定金額轉回
                for sub in selected_subs:
                    operations.append({
                        'type': 'sub_to_main',
                        'from_account': sub['id'],
                        'from_uuid': sub['uuid'],
                        'to_account': 'main',
                        'amount': transfer_amount_per_account,
                        'description': f"{sub['name']} → 主帳戶: {transfer_amount_per_account:.6f} {coin}"
                    })
                    
            except (ValueError, KeyboardInterrupt):
                print("[錯誤] 選擇無效或用戶取消")
                return None
    
    # 顯示操作計劃
    if not operations:
        print("\n[信息] 沒有需要執行的轉帳操作")
        return []
    
    print(f"\n[轉帳計劃]")
    total_amount = 0
    for op in operations:
        print(f"  {op['description']}")
        total_amount += op['amount']
    
    print(f"\n[總計] 將轉帳 {total_amount:.6f} {coin}")
    
    # 確認執行
    try:
        confirm = input(f"\n確認執行以上轉帳操作? (y/N): ").strip().lower()
        if confirm != 'y':
            print("[取消] 用戶取消操作")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    return operations


def transfer_step3_execute_operations(coin, operations):
    """步驟3: 執行轉帳操作"""
    print(f"\n=== 步驟3: 執行轉帳操作 ===")
    
    if not operations:
        print("[信息] 沒有操作需要執行")
        return True
    
    success_count = 0
    total_count = len(operations)
    
    print(f"[信息] 開始執行 {total_count} 個轉帳操作...")
    
    for i, op in enumerate(operations):
        transfer_type = op['type']
        amount = op['amount']
        description = op['description']
        
        print(f"\n[執行 {i+1}/{total_count}] {description}")
        
        try:
            if transfer_type == 'main_to_sub':
                # 主帳戶轉子帳戶
                result = transfer_to_subaccount(
                    coin=coin,
                    amount=amount,
                    sub_account_uid=op['to_uuid'],
                    account_key='main'
                )
            elif transfer_type == 'sub_to_main':
                # 子帳戶轉主帳戶 - 從配置中獲取主帳戶UID
                main_account_uid = get_main_account_uid()
                
                if main_account_uid:
                    result = transfer_to_main_account(
                        coin=coin,
                        amount=amount,
                        sub_account_uid=op['from_uuid'],
                        main_account_uid=main_account_uid,
                        account_key='main'
                    )
                else:
                    result = {'code': 'ERROR', 'msg': '配置中找不到主帳戶UID，請檢查配置'}
            else:
                print(f"[錯誤] 未知轉帳類型: {transfer_type}")
                continue
            
            # 檢查執行結果
            if result.get('code') == '00000':
                transfer_id = result.get('data', {}).get('transferId', '')
                print(f"  [OK] 成功 (轉帳ID: {transfer_id})")
                success_count += 1
            else:
                error_msg = result.get('msg', '未知錯誤')
                print(f"  [ERROR] 失敗: {error_msg}")
                print(f"     詳細: {result}")
            
            # 輕微延遲避免過於頻繁請求
            if i < total_count - 1:
                time.sleep(0.3)
                
        except Exception as e:
            print(f"  [ERROR] 異常: {e}")
    
    print(f"\n[完成] 轉帳操作執行完成")
    print(f"  成功: {success_count}/{total_count}")
    print(f"  失敗: {total_count - success_count}/{total_count}")
    
    return True


def transfer_step4_final_query(coin, original_balances):
    """步驟4: 再次查詢並顯示最終狀況"""
    print(f"\n=== 步驟4: 最終餘額查詢 ===")
    
    print(f"[等待] 等待5秒讓轉帳操作結算...")
    time.sleep(5)
    
    print(f"[查詢] {coin} 最新餘額...")
    
    # 重新查詢所有帳戶餘額
    final_balances = transfer_step1_query_balances(coin)
    if not final_balances:
        print("[錯誤] 無法查詢最終餘額")
        return False
    
    # 顯示前後對比
    print(f"\n=== 轉帳結果對比 ===")
    print(f"{'帳戶':<12} {'轉帳前':<15} {'轉帳後':<15} {'變化':<20}")
    print("-" * 65)
    
    total_before = 0
    total_after = 0
    
    for account_id in final_balances.keys():
        # 轉帳前餘額
        before_data = original_balances.get(account_id, {})
        before_balance = get_account_spot_balance(before_data)
        
        # 轉帳後餘額
        after_data = final_balances.get(account_id, {})
        after_balance = get_account_spot_balance(after_data)
        
        # 計算變化
        balance_change = after_balance - before_balance
        
        # 變化描述
        if abs(balance_change) < 0.000001:
            change_desc = "無變化"
        elif balance_change > 0:
            change_desc = f"轉入 +{balance_change:.6f}"
        else:
            change_desc = f"轉出 {balance_change:.6f}"
        
        # 帳戶名稱
        account_type = after_data.get('account_info', {}).get('type', '')
        account_name = "主帳戶" if account_type == 'main' else f"子帳戶{account_id}"
        
        print(f"{account_name:<12} {before_balance:<15.6f} {after_balance:<15.6f} {change_desc:<20}")
        
        # 累計統計
        total_before += before_balance
        total_after += after_balance
    
    # 顯示總計
    print("-" * 65)
    total_change = total_after - total_before
    if abs(total_change) < 0.000001:
        total_change_desc = "無變化"
    else:
        total_change_desc = f"淨變化 {total_change:+.6f}"
    
    print(f"{'總計':<12} {total_before:<15.6f} {total_after:<15.6f} {total_change_desc:<20}")
    
    return True


def get_account_spot_balance(account_data):
    """從帳戶數據中提取現貨可用餘額"""
    wallet_result = account_data.get('wallet_result', {})
    if wallet_result.get('code') == '00000' and wallet_result.get('data'):
        wallet_data = wallet_result.get('data', [])
        if wallet_data:
            return float(wallet_data[0].get('available', '0'))
    return 0.0


def ensure_main_account_uid():
    """確保主帳戶UID已記錄在配置中"""
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    # 檢查配置中是否已有主帳戶UID
    main_config = config.get('accounts', {}).get('main', {})
    main_uid = main_config.get('uuid')
    
    if main_uid:
        print(f"[信息] 主帳戶UID已存在: {main_uid}")
        return True
    
    # 如果沒有，調用API獲取並保存
    print("[信息] 配置中沒有主帳戶UID，正在查詢並記錄...")
    
    try:
        account_info_result = get_account_info('main')
        print(f"[DEBUG] 帳戶信息API返回: {account_info_result}")
        
        if account_info_result.get('code') == '00000':
            data = account_info_result.get('data', {})
            main_uid = data.get('userId')
            
            if main_uid:
                # 更新配置文件
                config['accounts']['main']['uuid'] = main_uid
                save_config(config)
                print(f"[成功] 已獲取並保存主帳戶UID: {main_uid}")
                return True
            else:
                print("[錯誤] API返回中沒有找到userId字段")
                print(f"[DEBUG] 完整data內容: {data}")
        else:
            print(f"[錯誤] 獲取帳戶信息失敗: {account_info_result.get('msg', '未知錯誤')}")
    except Exception as e:
        print(f"[錯誤] 獲取帳戶信息異常: {e}")
    
    print("[解決方案] 請手動在配置文件的main帳戶中添加'uuid'字段")
    return False


def get_main_account_uid():
    """從配置中獲取主帳戶UID"""
    config = load_config()
    if not config:
        return None
    
    main_config = config.get('accounts', {}).get('main', {})
    return main_config.get('uuid')


def show_menu():
    """顯示功能選單"""
    print("\n=== Bitget Flex Manager CLI ===")
    print("1. 初始化 - 完整設定所有子帳戶和API Key")
    print("2. 理財寶管理 - 主子帳戶理財寶批量操作")
    print("3. 轉帳管理 - 主子帳戶間資金轉移")
    print("0. 退出")
    print("================================")


def main():
    """主程式"""
    # 啟動時檢查版本更新（異步，不阻塞主程序）
    print("[信息] 檢查版本更新中...")
    check_for_updates()
    
    while True:
        show_menu()
        choice = input("請選擇功能: ").strip()
        
        if choice == '0':
            print("再見!")
            break
        elif choice == '1':
            print("\n[執行] 完整初始化功能...")
            
            # 步驟0: 選擇配置文件
            print("\n[步驟0] 選擇配置文件...")
            test_config = load_config(allow_file_selection=True)
            if not test_config:
                print("[初始化失敗] 無法載入配置文件")
                continue
            
            # 步驟1: 確保有指定數量的子帳戶
            subaccount_success = ensure_target_subaccounts()
            if not subaccount_success:
                print("[初始化失敗] 子帳戶設定失敗")
                continue
            
            # 步驟2: 創建API Key
            print("\n" + "="*50)
            apikey_success = create_apikeys_for_subaccounts()
            
            # 完成總結
            print("\n" + "="*50)
            if apikey_success:
                print("[全部完成] 初始化完成！")
                print("   [OK] 子帳戶設定完成")
                print("   [OK] API Key 設定完成")
                print("   [OK] 配置文件已更新")
            else:
                print("[部分完成] 初始化部分完成")
                print("   [OK] 子帳戶設定完成")
                print("   [ERROR] API Key 創建有問題")
            print("="*50)
        elif choice == '2':
            print("\n[執行] 理財寶管理功能...")
            savings_success = savings_management_workflow()
            if savings_success:
                print("\n[理財寶管理完成]")
            else:
                print("\n[理財寶管理失敗] 請檢查錯誤信息")
        elif choice == '3':
            print("\n[執行] 轉帳管理功能...")
            transfer_success = transfer_management_workflow()
            if transfer_success:
                print("\n[轉帳管理完成]")
            else:
                print("\n[轉帳管理失敗] 請檢查錯誤信息")
        else:
            print("[錯誤] 無效選擇，請重新輸入")


if __name__ == "__main__":
    main()