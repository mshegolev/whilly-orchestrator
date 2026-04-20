"""
Resource monitoring and protection system for Whilly orchestrator.
Prevents CPU/RAM/HDD resource leaks and system overload.
"""

import os
import platform
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import psutil
except ImportError:
    psutil = None


@dataclass
class ResourceLimits:
    """Resource usage limits and thresholds."""

    # CPU limits (percentage)
    max_cpu_percent: float = 80.0  # Max total CPU usage
    max_process_cpu: float = 50.0  # Max CPU per process

    # Memory limits (percentage of total RAM)
    max_memory_percent: float = 75.0  # Max total memory usage
    max_process_memory_mb: int = 2048  # Max memory per process (MB)

    # Disk limits
    min_free_space_gb: float = 5.0  # Min free disk space (GB)
    max_log_dir_size_gb: float = 2.0  # Max log directory size (GB)

    # Process limits
    max_concurrent_processes: int = 5  # Max whilly processes
    process_timeout_minutes: int = 30  # Max process runtime

    # Check intervals
    monitor_interval_seconds: int = 10  # Resource check frequency


@dataclass
class ResourceUsage:
    """Current system resource usage snapshot."""

    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_gb: float = 0.0
    memory_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    active_processes: int = 0
    timestamp: float = 0.0


class ResourceMonitor:
    """Monitors and protects system resources from Whilly processes."""

    def __init__(self, limits: Optional[ResourceLimits] = None, log_dir: str = "whilly_logs"):
        self.limits = limits or ResourceLimits()
        self.log_dir = Path(log_dir)
        self.startup_time = time.time()
        self.last_warning = 0.0  # Throttle warning messages
        self.warning_cooldown = 60.0  # Minimum seconds between warnings

        # Create log directory if needed
        self.log_dir.mkdir(exist_ok=True)

    def get_system_usage(self) -> ResourceUsage:
        """Get current system resource usage."""
        usage = ResourceUsage(timestamp=time.time())

        try:
            if psutil:
                # CPU usage
                usage.cpu_percent = psutil.cpu_percent(interval=0.1)

                # Memory usage
                memory = psutil.virtual_memory()
                usage.memory_percent = memory.percent
                usage.memory_used_gb = memory.used / (1024**3)
                usage.memory_total_gb = memory.total / (1024**3)

                # Disk usage for current working directory
                disk = psutil.disk_usage(".")
                usage.disk_free_gb = disk.free / (1024**3)
                usage.disk_total_gb = disk.total / (1024**3)

                # Count whilly processes
                usage.active_processes = self._count_whilly_processes()
            else:
                # Fallback without psutil
                usage = self._get_usage_fallback()

        except Exception as e:
            self._log_error(f"Failed to get system usage: {e}")

        return usage

    def _count_whilly_processes(self) -> int:
        """Count currently running whilly-related processes."""
        count = 0
        try:
            if psutil:
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        cmdline = proc.info.get("cmdline") or []
                        cmdline_str = " ".join(cmdline)
                        if (
                            "whilly" in cmdline_str.lower()
                            or "claude" in cmdline_str.lower()
                            and "whilly" in cmdline_str
                        ):
                            count += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
        except Exception:
            pass
        return count

    def _get_usage_fallback(self) -> ResourceUsage:
        """Get basic usage without psutil (less accurate)."""
        usage = ResourceUsage(timestamp=time.time())

        try:
            # Basic disk space check
            if platform.system() == "Windows":
                import shutil

                total, used, free = shutil.disk_usage(".")
                usage.disk_free_gb = free / (1024**3)
                usage.disk_total_gb = total / (1024**3)
            else:
                # Use 'df' command on Unix-like systems
                result = subprocess.run(["df", "."], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    if len(lines) >= 2:
                        fields = lines[1].split()
                        if len(fields) >= 4:
                            # df output: blocks of 1024 bytes
                            usage.disk_free_gb = int(fields[3]) / (1024**2)
                            usage.disk_total_gb = int(fields[1]) / (1024**2)

            # Basic process count
            if platform.system() != "Windows":
                result = subprocess.run(["pgrep", "-f", "whilly"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    usage.active_processes = len(result.stdout.strip().split("\n"))

        except Exception:
            pass

        return usage

    def check_limits(self, usage: Optional[ResourceUsage] = None) -> Dict[str, Any]:
        """Check if current usage exceeds limits. Returns violations dict."""
        if usage is None:
            usage = self.get_system_usage()

        violations = {}

        # CPU check
        if usage.cpu_percent > self.limits.max_cpu_percent:
            violations["cpu"] = {
                "current": usage.cpu_percent,
                "limit": self.limits.max_cpu_percent,
                "severity": "high" if usage.cpu_percent > 90 else "medium",
            }

        # Memory check
        if usage.memory_percent > self.limits.max_memory_percent:
            violations["memory"] = {
                "current": usage.memory_percent,
                "limit": self.limits.max_memory_percent,
                "severity": "high" if usage.memory_percent > 90 else "medium",
            }

        # Disk space check
        if usage.disk_free_gb < self.limits.min_free_space_gb:
            violations["disk"] = {
                "current": usage.disk_free_gb,
                "limit": self.limits.min_free_space_gb,
                "severity": "high" if usage.disk_free_gb < 1 else "medium",
            }

        # Process count check
        if usage.active_processes > self.limits.max_concurrent_processes:
            violations["processes"] = {
                "current": usage.active_processes,
                "limit": self.limits.max_concurrent_processes,
                "severity": "medium",
            }

        # Log directory size check
        log_size = self._get_log_dir_size_gb()
        if log_size > self.limits.max_log_dir_size_gb:
            violations["log_dir"] = {"current": log_size, "limit": self.limits.max_log_dir_size_gb, "severity": "low"}

        return violations

    def _get_log_dir_size_gb(self) -> float:
        """Get log directory size in GB."""
        try:
            total_size = 0
            for file_path in self.log_dir.rglob("*"):
                if file_path.is_file():
                    total_size += file_path.stat().st_size
            return total_size / (1024**3)
        except Exception:
            return 0.0

    def should_throttle(self, usage: Optional[ResourceUsage] = None) -> bool:
        """Check if we should throttle/pause new processes due to resource usage."""
        violations = self.check_limits(usage)

        # Throttle if any high severity violations
        high_severity = [v for v in violations.values() if v.get("severity") == "high"]
        if high_severity:
            return True

        # Throttle if too many medium severity violations
        medium_severity = [v for v in violations.values() if v.get("severity") == "medium"]
        if len(medium_severity) >= 2:
            return True

        return False

    def wait_for_resources(self, max_wait_seconds: int = 300) -> bool:
        """Wait for resource usage to drop below limits. Returns True if successful."""
        start_time = time.time()

        while time.time() - start_time < max_wait_seconds:
            usage = self.get_system_usage()

            if not self.should_throttle(usage):
                return True

            # Log warning periodically
            now = time.time()
            if now - self.last_warning > self.warning_cooldown:
                violations = self.check_limits(usage)
                self._log_warning(f"Waiting for resources. Violations: {violations}")
                self.last_warning = now

            time.sleep(self.limits.monitor_interval_seconds)

        return False

    def cleanup_logs(self) -> int:
        """Clean up old log files. Returns number of files removed."""
        removed = 0
        try:
            # Remove logs older than 7 days
            cutoff_time = time.time() - (7 * 24 * 60 * 60)

            for log_file in self.log_dir.rglob("*.log"):
                if log_file.stat().st_mtime < cutoff_time:
                    log_file.unlink()
                    removed += 1

            # Remove empty directories
            for dir_path in self.log_dir.rglob("*"):
                if dir_path.is_dir() and not list(dir_path.iterdir()):
                    dir_path.rmdir()

        except Exception as e:
            self._log_error(f"Failed to cleanup logs: {e}")

        return removed

    def get_recommendation(self, violations: Dict[str, Any]) -> str:
        """Get human-readable recommendation for resolving resource issues."""
        if not violations:
            return "✅ All resource usage within limits"

        recommendations = []

        if "cpu" in violations:
            recommendations.append(
                f"🔴 CPU usage too high ({violations['cpu']['current']:.1f}%). "
                f"Consider reducing WHILLY_MAX_PARALLEL or waiting for processes to complete."
            )

        if "memory" in violations:
            recommendations.append(
                f"🔴 Memory usage too high ({violations['memory']['current']:.1f}%). "
                f"Close other applications or reduce concurrent processes."
            )

        if "disk" in violations:
            recommendations.append(
                f"🔴 Low disk space ({violations['disk']['current']:.1f}GB free). "
                f"Free up disk space or change working directory."
            )

        if "processes" in violations:
            recommendations.append(
                f"🔴 Too many processes ({violations['processes']['current']}). Wait for some processes to complete."
            )

        if "log_dir" in violations:
            recommendations.append(
                f"⚠️ Log directory large ({violations['log_dir']['current']:.1f}GB). Consider cleaning up old logs."
            )

        return " ".join(recommendations)

    def _log_warning(self, message: str):
        """Log a warning message."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_file = self.log_dir / "resource_monitor.log"
        try:
            with open(log_file, "a") as f:
                f.write(f"[{timestamp}] WARNING: {message}\n")
        except Exception:
            pass

    def _log_error(self, message: str):
        """Log an error message."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_file = self.log_dir / "resource_monitor.log"
        try:
            with open(log_file, "a") as f:
                f.write(f"[{timestamp}] ERROR: {message}\n")
        except Exception:
            pass


def create_monitor_from_env() -> ResourceMonitor:
    """Create ResourceMonitor from environment variables."""
    limits = ResourceLimits()

    # Load limits from environment
    if val := os.environ.get("WHILLY_MAX_CPU_PERCENT"):
        limits.max_cpu_percent = float(val)
    if val := os.environ.get("WHILLY_MAX_MEMORY_PERCENT"):
        limits.max_memory_percent = float(val)
    if val := os.environ.get("WHILLY_MIN_FREE_SPACE_GB"):
        limits.min_free_space_gb = float(val)
    if val := os.environ.get("WHILLY_PROCESS_TIMEOUT_MINUTES"):
        limits.process_timeout_minutes = int(val)

    log_dir = os.environ.get("WHILLY_LOG_DIR", "whilly_logs")

    return ResourceMonitor(limits, log_dir)


# Module-level monitor instance
_monitor: Optional[ResourceMonitor] = None


def get_monitor() -> ResourceMonitor:
    """Get global ResourceMonitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = create_monitor_from_env()
    return _monitor
