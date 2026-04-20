"""Tests for resource monitoring and protection system."""

import time
from pathlib import Path
from unittest.mock import Mock, patch


from whilly.resource_monitor import (
    ResourceLimits,
    ResourceMonitor,
    ResourceUsage,
    create_monitor_from_env,
    get_monitor,
)


def test_resource_limits_defaults():
    """Test ResourceLimits default values."""
    limits = ResourceLimits()
    assert limits.max_cpu_percent == 80.0
    assert limits.max_memory_percent == 75.0
    assert limits.min_free_space_gb == 5.0
    assert limits.max_process_memory_mb == 2048
    assert limits.max_concurrent_processes == 5
    assert limits.process_timeout_minutes == 30


def test_resource_usage_defaults():
    """Test ResourceUsage default values."""
    usage = ResourceUsage()
    assert usage.cpu_percent == 0.0
    assert usage.memory_percent == 0.0
    assert usage.active_processes == 0
    assert usage.timestamp == 0.0


def test_resource_monitor_init():
    """Test ResourceMonitor initialization."""
    monitor = ResourceMonitor()
    assert isinstance(monitor.limits, ResourceLimits)
    assert monitor.log_dir.name == "whilly_logs"
    assert monitor.startup_time > 0
    assert monitor.last_warning == 0.0
    assert monitor.warning_cooldown == 60.0


def test_resource_monitor_custom_limits():
    """Test ResourceMonitor with custom limits."""
    limits = ResourceLimits(max_cpu_percent=90.0, max_memory_percent=85.0)
    monitor = ResourceMonitor(limits)
    assert monitor.limits.max_cpu_percent == 90.0
    assert monitor.limits.max_memory_percent == 85.0


@patch("whilly.resource_monitor.psutil")
def test_get_system_usage_with_psutil(mock_psutil):
    """Test get_system_usage when psutil is available."""
    # Mock psutil functions
    mock_psutil.cpu_percent.return_value = 45.5

    mock_memory = Mock()
    mock_memory.percent = 60.2
    mock_memory.used = 8 * 1024**3  # 8GB
    mock_memory.total = 16 * 1024**3  # 16GB
    mock_psutil.virtual_memory.return_value = mock_memory

    mock_disk = Mock()
    mock_disk.free = 100 * 1024**3  # 100GB
    mock_disk.total = 500 * 1024**3  # 500GB
    mock_psutil.disk_usage.return_value = mock_disk

    # Mock process iteration
    mock_proc = Mock()
    mock_proc.info = {"cmdline": ["python", "-m", "whilly", "--tasks", "test.json"]}
    mock_psutil.process_iter.return_value = [mock_proc]

    monitor = ResourceMonitor()
    monitor._count_whilly_processes = Mock(return_value=2)

    usage = monitor.get_system_usage()

    assert usage.cpu_percent == 45.5
    assert usage.memory_percent == 60.2
    assert usage.memory_used_gb == 8.0
    assert usage.memory_total_gb == 16.0
    assert usage.disk_free_gb == 100.0
    assert usage.disk_total_gb == 500.0
    assert usage.active_processes == 2
    assert usage.timestamp > 0


def test_get_system_usage_fallback():
    """Test get_system_usage fallback when psutil is not available."""
    # Temporarily disable psutil import
    with patch("whilly.resource_monitor.psutil", None):
        monitor = ResourceMonitor()
        usage = monitor.get_system_usage()

        # Should return basic usage with zeros/defaults
        assert usage.cpu_percent == 0.0
        assert usage.memory_percent == 0.0
        assert usage.timestamp > 0


def test_check_limits_no_violations():
    """Test check_limits with usage within limits."""
    monitor = ResourceMonitor()
    usage = ResourceUsage(cpu_percent=50.0, memory_percent=60.0, disk_free_gb=10.0, active_processes=3)

    violations = monitor.check_limits(usage)
    assert len(violations) == 0


def test_check_limits_with_violations():
    """Test check_limits with various violations."""
    monitor = ResourceMonitor()
    usage = ResourceUsage(
        cpu_percent=85.0,  # > 80% limit
        memory_percent=80.0,  # > 75% limit
        disk_free_gb=2.0,  # < 5GB limit
        active_processes=7,  # > 5 limit
    )

    violations = monitor.check_limits(usage)

    assert "cpu" in violations
    assert violations["cpu"]["current"] == 85.0
    assert violations["cpu"]["limit"] == 80.0
    assert violations["cpu"]["severity"] == "medium"

    assert "memory" in violations
    assert violations["memory"]["current"] == 80.0
    assert violations["memory"]["limit"] == 75.0

    assert "disk" in violations
    assert violations["disk"]["current"] == 2.0
    assert violations["disk"]["limit"] == 5.0

    assert "processes" in violations
    assert violations["processes"]["current"] == 7
    assert violations["processes"]["limit"] == 5


def test_should_throttle():
    """Test should_throttle logic."""
    monitor = ResourceMonitor()

    # No throttling for normal usage
    usage_ok = ResourceUsage(
        cpu_percent=50.0,
        memory_percent=60.0,
        disk_free_gb=10.0,  # Above minimum
        active_processes=3,  # Below limit
    )
    assert not monitor.should_throttle(usage_ok)

    # Throttle for high severity violations
    usage_high = ResourceUsage(cpu_percent=95.0)  # High severity
    assert monitor.should_throttle(usage_high)

    # Throttle for multiple medium severity violations
    usage_medium = ResourceUsage(
        cpu_percent=85.0,  # Medium severity
        memory_percent=80.0,  # Medium severity
        active_processes=7,  # Medium severity
    )
    assert monitor.should_throttle(usage_medium)


def test_get_recommendation():
    """Test get_recommendation message generation."""
    monitor = ResourceMonitor()

    # No violations
    assert "✅ All resource usage within limits" in monitor.get_recommendation({})

    # CPU violation
    violations = {"cpu": {"current": 85.0, "limit": 80.0, "severity": "medium"}}
    recommendation = monitor.get_recommendation(violations)
    assert "🔴 CPU usage too high" in recommendation
    assert "reducing WHILLY_MAX_PARALLEL" in recommendation

    # Memory violation
    violations = {"memory": {"current": 80.0, "limit": 75.0, "severity": "medium"}}
    recommendation = monitor.get_recommendation(violations)
    assert "🔴 Memory usage too high" in recommendation
    assert "Close other applications" in recommendation


@patch.dict(
    "os.environ",
    {
        "WHILLY_MAX_CPU_PERCENT": "90.0",
        "WHILLY_MAX_MEMORY_PERCENT": "85.0",
        "WHILLY_MIN_FREE_SPACE_GB": "10.0",
        "WHILLY_LOG_DIR": "custom_logs",
    },
)
def test_create_monitor_from_env():
    """Test creating monitor from environment variables."""
    monitor = create_monitor_from_env()

    assert monitor.limits.max_cpu_percent == 90.0
    assert monitor.limits.max_memory_percent == 85.0
    assert monitor.limits.min_free_space_gb == 10.0
    assert monitor.log_dir == Path("custom_logs")


def test_get_monitor_singleton():
    """Test get_monitor returns singleton instance."""
    # Clear any existing monitor
    import whilly.resource_monitor

    whilly.resource_monitor._monitor = None

    monitor1 = get_monitor()
    monitor2 = get_monitor()

    assert monitor1 is monitor2


def test_wait_for_resources_timeout():
    """Test wait_for_resources with timeout."""
    monitor = ResourceMonitor()

    # Mock should_throttle to always return True
    monitor.should_throttle = Mock(return_value=True)
    monitor.get_system_usage = Mock(return_value=ResourceUsage())

    # Should timeout after max_wait_seconds
    start_time = time.time()
    result = monitor.wait_for_resources(max_wait_seconds=1)
    elapsed = time.time() - start_time

    assert not result  # Should return False on timeout
    assert elapsed >= 1.0  # Should wait at least the specified time


def test_wait_for_resources_success():
    """Test wait_for_resources when resources become available."""
    monitor = ResourceMonitor()

    # Mock should_throttle to return False (resources available)
    monitor.should_throttle = Mock(return_value=False)
    monitor.get_system_usage = Mock(return_value=ResourceUsage())

    result = monitor.wait_for_resources(max_wait_seconds=10)
    assert result  # Should return True when resources available


def test_cleanup_logs(tmp_path):
    """Test log cleanup functionality."""
    log_dir = tmp_path / "test_logs"
    log_dir.mkdir()

    # Create some test log files
    old_log = log_dir / "old.log"
    new_log = log_dir / "new.log"

    old_log.write_text("old log content")
    new_log.write_text("new log content")

    # Make old log appear old by modifying its timestamp
    import os

    old_time = time.time() - (8 * 24 * 60 * 60)  # 8 days ago
    os.utime(old_log, (old_time, old_time))

    monitor = ResourceMonitor(log_dir=str(log_dir))
    removed = monitor.cleanup_logs()

    assert removed == 1  # Should remove 1 old file
    assert not old_log.exists()  # Old log should be removed
    assert new_log.exists()  # New log should remain
