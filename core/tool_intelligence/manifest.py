from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from enum import Enum

class ToolCategory(str, Enum):
    EXECUTION = "Execution"
    MEMORY = "Memory"
    ORCHESTRATION = "Orchestration"
    PLANNING = "Planning"

class ToolSecurity(BaseModel):
    requires_sandbox: bool = True
    requires_network: bool = False
    read_only: bool = False
    max_timeout: int = 120

class ToolManifest(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str
    use_cases: List[str] = []
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}
    output_format: str = "text"
    capabilities: List[str] = []
    category: ToolCategory = ToolCategory.EXECUTION
    security: ToolSecurity = Field(default_factory=ToolSecurity)
    implementation_type: str = "python"  # python | cli
    cli_command: Optional[str] = None
    cli_binary: Optional[str] = None
    dependencies: List[str] = []
    timeout: int = 60
    author: Optional[str] = None
    source: Optional[str] = None

    def to_dict(self) -> dict:
        return self.model_dump()
