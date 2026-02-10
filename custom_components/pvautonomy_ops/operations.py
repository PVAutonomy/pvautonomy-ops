"""Operations Manager for PVAutonomy Ops.

Handles safe operation execution with:
- Single-flight lock (no concurrent operations)
- Operation lifecycle tracking (idle → running → success/fail)
- Progress reporting for long-running operations
- Audit logging

Contract: ops-contract-v1.md v1.0.0 + Phase 3 extensions
"""
import asyncio
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)


class OperationState(str, Enum):
    """Operation lifecycle states (Phase 3 extension)."""
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class OperationLock:
    """Single-flight operation lock.
    
    Ensures only ONE operation runs at a time across all actions.
    """
    
    def __init__(self):
        """Initialize operation lock."""
        self._lock = asyncio.Lock()
        self._current_operation: Optional[str] = None
        self._operation_started: Optional[datetime] = None
    
    @property
    def is_locked(self) -> bool:
        """Check if operation is running."""
        return self._lock.locked()
    
    @property
    def current_operation(self) -> Optional[str]:
        """Get current operation name."""
        return self._current_operation
    
    async def acquire(self, operation_name: str) -> bool:
        """Acquire lock for operation.
        
        Args:
            operation_name: Name of operation requesting lock
            
        Returns:
            True if lock acquired, False if already locked
        """
        if self._lock.locked():
            _LOGGER.warning(
                "Operation '%s' blocked: '%s' already running since %s",
                operation_name,
                self._current_operation,
                self._operation_started
            )
            return False
        
        await self._lock.acquire()
        self._current_operation = operation_name
        self._operation_started = datetime.now()
        _LOGGER.info("Operation lock acquired: %s", operation_name)
        return True
    
    def release(self, operation_name: str):
        """Release operation lock.
        
        Args:
            operation_name: Name of operation releasing lock
        """
        if self._current_operation == operation_name:
            self._current_operation = None
            self._operation_started = None
            self._lock.release()
            _LOGGER.info("Operation lock released: %s", operation_name)
        else:
            _LOGGER.error(
                "Lock release mismatch: current='%s', requested='%s'",
                self._current_operation,
                operation_name
            )


class OperationTracker:
    """Tracks operation lifecycle and progress.
    
    Phase 3 extension attributes:
    - op_state: idle|queued|running|success|failed
    - op_name: discover|flash|gates|migrate|restart
    - op_started, op_finished (ISO8601)
    - op_progress (0-100)
    - last_error, last_error_time
    """
    
    def __init__(self, hass: HomeAssistant):
        """Initialize operation tracker.
        
        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self._state = OperationState.IDLE
        self._name: Optional[str] = None
        self._started: Optional[datetime] = None
        self._finished: Optional[datetime] = None
        self._progress: int = 0
        self._last_error: Optional[str] = None
        self._last_error_time: Optional[datetime] = None
        
        # Contract v1.0.0 compatibility
        self._last_action: Optional[str] = None
        self._last_action_time: Optional[datetime] = None
    
    def start_operation(self, operation_name: str):
        """Mark operation as started.
        
        Args:
            operation_name: Name of operation (discover, flash, gates, etc.)
        """
        self._state = OperationState.RUNNING
        self._name = operation_name
        self._started = datetime.now()
        self._finished = None
        self._progress = 0
        
        _LOGGER.info("Operation started: %s", operation_name)
        self.hass.bus.async_fire(
            "pvautonomy_ops_operation_started",
            {"operation": operation_name, "started": self._started.isoformat()}
        )
    
    def update_progress(self, progress: int, message: Optional[str] = None):
        """Update operation progress.
        
        Args:
            progress: Progress percentage (0-100)
            message: Optional progress message
        """
        self._progress = max(0, min(100, progress))
        
        if message:
            _LOGGER.debug("Operation progress: %d%% - %s", self._progress, message)
        else:
            _LOGGER.debug("Operation progress: %d%%", self._progress)
        
        self.hass.bus.async_fire(
            "pvautonomy_ops_operation_progress",
            {"progress": self._progress, "message": message}
        )
    
    def complete_operation(self, success: bool, error: Optional[str] = None):
        """Mark operation as completed.
        
        Args:
            success: True if operation succeeded
            error: Error message if operation failed
        """
        self._state = OperationState.SUCCESS if success else OperationState.FAILED
        self._finished = datetime.now()
        self._progress = 100 if success else self._progress
        
        # Update contract v1.0.0 attributes
        self._last_action = self._name
        self._last_action_time = self._finished
        
        if not success:
            self._last_error = error
            self._last_error_time = self._finished
            _LOGGER.error("Operation failed: %s - %s", self._name, error)
        else:
            _LOGGER.info("Operation completed: %s", self._name)
        
        self.hass.bus.async_fire(
            "pvautonomy_ops_operation_completed",
            {
                "operation": self._name,
                "success": success,
                "error": error,
                "finished": self._finished.isoformat(),
                "duration_ms": self.duration_ms
            }
        )
    
    def reset(self):
        """Reset tracker to idle state."""
        self._state = OperationState.IDLE
        self._name = None
        self._started = None
        self._finished = None
        self._progress = 0
    
    @property
    def state(self) -> OperationState:
        """Get current operation state."""
        return self._state
    
    @property
    def is_running(self) -> bool:
        """Check if operation is currently running."""
        return self._state == OperationState.RUNNING
    
    @property
    def duration_ms(self) -> Optional[int]:
        """Get operation duration in milliseconds."""
        if not self._started:
            return None
        
        end_time = self._finished or datetime.now()
        delta = end_time - self._started
        return int(delta.total_seconds() * 1000)
    
    def to_dict(self) -> dict[str, Any]:
        """Export tracker state as dict (for sensor attributes).
        
        Returns:
            Dict with Phase 3 + Contract v1.0.0 attributes
        """
        return {
            # Phase 3 extensions
            "op_state": self._state.value,
            "op_name": self._name,
            "op_started": self._started.isoformat() if self._started else None,
            "op_finished": self._finished.isoformat() if self._finished else None,
            "op_progress": self._progress,
            "op_duration_ms": self.duration_ms,
            
            # Contract v1.0.0 compatibility
            "last_action": self._last_action,
            "last_action_time": self._last_action_time.isoformat() if self._last_action_time else None,
            "last_error": self._last_error,
            "last_error_time": self._last_error_time.isoformat() if self._last_error_time else None,
        }


class OperationRunner:
    """High-level operation runner with lock + tracking.
    
    Usage:
        runner = OperationRunner(hass, tracker, lock)
        result = await runner.run("discover", discover_func, arg1, arg2)
    """
    
    def __init__(
        self,
        hass: HomeAssistant,
        tracker: OperationTracker,
        lock: OperationLock
    ):
        """Initialize operation runner.
        
        Args:
            hass: Home Assistant instance
            tracker: Operation tracker
            lock: Operation lock
        """
        self.hass = hass
        self.tracker = tracker
        self.lock = lock
    
    async def run(
        self,
        operation_name: str,
        operation_func: Callable,
        *args,
        **kwargs
    ) -> dict[str, Any]:
        """Run operation with lock and lifecycle tracking.
        
        Args:
            operation_name: Name of operation
            operation_func: Async function to execute
            *args: Positional arguments for operation_func
            **kwargs: Keyword arguments for operation_func
            
        Returns:
            Operation result dict with:
                - success: bool
                - result: Any (operation return value)
                - error: str|None
                - duration_ms: int
        """
        # Try to acquire lock
        if not await self.lock.acquire(operation_name):
            return {
                "success": False,
                "result": None,
                "error": f"Operation blocked: {self.lock.current_operation} already running",
                "duration_ms": 0
            }
        
        try:
            # Start tracking
            self.tracker.start_operation(operation_name)
            
            # Execute operation
            result = await operation_func(*args, **kwargs)
            
            # Mark success
            self.tracker.complete_operation(success=True)
            
            return {
                "success": True,
                "result": result,
                "error": None,
                "duration_ms": self.tracker.duration_ms
            }
            
        except Exception as ex:
            # Mark failure
            error_msg = f"{type(ex).__name__}: {str(ex)}"
            self.tracker.complete_operation(success=False, error=error_msg)
            
            _LOGGER.exception("Operation '%s' failed", operation_name)
            
            return {
                "success": False,
                "result": None,
                "error": error_msg,
                "duration_ms": self.tracker.duration_ms
            }
            
        finally:
            # Always release lock
            self.lock.release(operation_name)
