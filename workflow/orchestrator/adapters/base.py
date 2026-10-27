"""Abstract base class for Antigravity integration adapters."""

from abc import ABC, abstractmethod
from ..models import TaskExecutionSpec, TaskExecutionResult


class AntigravityAdapter(ABC):
    """Abstract interface defining the contract for launching AI agent task execution."""

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Check if this adapter's underlying runtime (SDK/CLI/Binary) is installed and operational.
        
        Returns:
            tuple[bool, str]: (is_ready, diagnostic_details)
        """
        pass

    @abstractmethod
    def execute_task(self, spec: TaskExecutionSpec) -> TaskExecutionResult:
        """Execute exactly one approved task within its declared scope.
        
        Args:
            spec: Full task execution specification.
            
        Returns:
            TaskExecutionResult containing execution status, output, and verification results.
        """
        pass
