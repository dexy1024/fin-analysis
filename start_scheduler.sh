#!/bin/bash
# 启动调度器脚本 - 清除缓存后启动

cd /Users/yuguoq/Desktop/CursorProject/fin-analysis/backend

# 1. 清除Python缓存
echo "清除Python缓存..."
find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null

# 2. 清除调度器锁文件
echo "清除锁文件..."
rm -f /tmp/kline_scheduler.lock /tmp/kline_scheduler_status.json

# 3. 杀死旧进程
echo "清理旧进程..."
pkill -9 -f "python.*kline.*scheduler" 2>/dev/null
sleep 2

# 4. 启动调度器
echo "启动调度器..."
nohup python3 -c "
from main import setup_kline_scheduler, set_sse_callback
set_sse_callback(lambda x,y: None)
setup_kline_scheduler()
print('调度器已启动', flush=True)
import time
while True:
    time.sleep(60)
" > /tmp/scheduler.log 2>&1 &

PID=$!
echo "调度器PID: $PID"
sleep 3

# 5. 检查状态
if ps -p $PID > /dev/null 2>&1; then
    echo "调度器启动成功"
    tail -5 /tmp/scheduler.log
else
    echo "启动失败，查看日志:"
    tail -10 /tmp/scheduler.log
fi
