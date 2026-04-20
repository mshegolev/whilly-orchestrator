# Resource Protection System

Whilly includes comprehensive resource monitoring and protection to prevent CPU/RAM/HDD leaks and system overload.

## 🛡️ Protection Features

- **CPU Monitoring**: Prevents excessive CPU usage
- **Memory Protection**: Monitors RAM consumption 
- **Disk Space**: Ensures minimum free space
- **Process Limits**: Controls concurrent processes
- **Auto-Throttling**: Pauses when limits exceeded

## ⚙️ Configuration

Set limits via environment variables:

```bash
# CPU limits
export WHILLY_MAX_CPU_PERCENT=80          # Max total CPU (default: 80%)

# Memory limits  
export WHILLY_MAX_MEMORY_PERCENT=75       # Max memory usage (default: 75%)

# Disk limits
export WHILLY_MIN_FREE_SPACE_GB=5         # Min free space (default: 5GB)

# Process limits
export WHILLY_PROCESS_TIMEOUT_MINUTES=30  # Max runtime (default: 30min)
export WHILLY_MAX_PARALLEL=2              # Max concurrent agents (default: 3)

# Control
export WHILLY_RESOURCE_CHECK_ENABLED=1    # Enable monitoring (default: true)
```

## 🚨 Behavior When Limits Exceeded

When resource usage exceeds limits:

1. **High Severity** (CPU >90%, Memory >90%, Disk <1GB):
   - ❌ **Blocks** new processes immediately
   - Waits up to 5 minutes for resources
   - Aborts if resources don't become available

2. **Medium Severity** (Multiple violations):
   - ⏸️ **Throttles** new processes
   - Shows warning with recommendations
   - Waits for improvement

3. **Low Severity** (Single minor violation):
   - ⚠️ **Warns** but continues
   - Logs recommendations

## 📊 Monitoring Output

Resource warnings show current usage and recommendations:

```
⚠️  Resource warning at startup:
   🔴 CPU usage too high (85.0%). Consider reducing WHILLY_MAX_PARALLEL or waiting for processes to complete.
   🔴 Memory usage too high (80.0%). Close other applications or reduce concurrent processes.
```

## 🔧 Troubleshooting

### High CPU Usage
- Reduce `WHILLY_MAX_PARALLEL` to 1-2
- Close other CPU-intensive applications
- Wait for current processes to complete

### High Memory Usage  
- Close unnecessary applications
- Reduce `WHILLY_MAX_PARALLEL`
- Free system memory

### Low Disk Space
- Clean up temporary files
- Remove old logs: `rm -rf whilly_logs/old*`
- Free disk space in working directory

### Too Many Processes
- Wait for current agents to complete
- Kill stuck processes: `pkill -f whilly`
- Reduce `WHILLY_MAX_PARALLEL`

## 🧹 Log Cleanup

Resource monitor automatically:
- Removes logs older than 7 days
- Cleans empty directories
- Monitors log directory size

Manual cleanup:
```bash
# Clean old logs
find whilly_logs/ -name "*.log" -mtime +7 -delete

# Check log directory size
du -sh whilly_logs/
```

## 🚀 Dependencies

- **psutil** (recommended): Full resource monitoring
- **Fallback mode**: Basic monitoring without psutil

Install psutil for best experience:
```bash
pip install 'psutil>=5.9.0'
```

## 📈 Performance Impact

Resource monitoring has minimal overhead:
- Checks every 10 seconds during execution
- ~1ms per check with psutil
- ~10ms per check in fallback mode
- No impact when disabled

## 🎛️ Advanced Configuration

For specialized environments:

```bash
# Custom resource limits
export WHILLY_MAX_PROCESS_MEMORY_MB=4096   # Max memory per process
export WHILLY_MAX_LOG_DIR_SIZE_GB=1        # Max log directory size

# Monitoring frequency
export WHILLY_MONITOR_INTERVAL_SECONDS=5   # Check interval
```

## 🔒 Security

Resource protection prevents:
- System crashes from runaway processes
- Out-of-memory conditions
- Disk space exhaustion
- CPU starvation of other applications

Safe to run on shared systems and CI environments.