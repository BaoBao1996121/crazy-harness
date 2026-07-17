from typing import Literal

from pydantic import BaseModel, ValidationError, model_validator


class Command(BaseModel):
    type: Literal["call_tool", "submit"]
    tool_name: str | None = None

    @model_validator(mode="after")
    def require_tool_name(self):
        if self.type == "call_tool" and not self.tool_name:
            raise ValueError("tool_name required")
        return self


try:
    Command(type="call_tool")
except ValidationError:
    print("strict_tool_command=ok")
else:
    raise AssertionError("invalid tool command was accepted")
