#!/usr/bin/env python
# Path: main.py
"""
AIAutoStocks 根目錄執行檔入口
允許直接在專案根目錄下運行：
python main.py --mode live --stocks 2330,2454
"""
import sys
from src.main import main

if __name__ == "__main__":
    main()
