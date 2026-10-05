from core.workspace import Workspace
from core.sandbox import Sandbox
from core.memory import Memory
import os
import re

sandbox = Sandbox()
memory = Memory()

class Tools:

    @staticmethod
    def read_file(path: str) -> str:
        try:
            return Workspace.resolve(path).read_text()
        except Exception as e:
            return str(e)

    @staticmethod
    def write_file(path: str, content: str) -> str:
        try:
            Workspace.resolve(path).write_text(content)
            return "OK"
        except Exception as e:
            return str(e)

    @staticmethod
    def list_directory(path: str) -> str:
        try:
            return "\n".join(os.listdir(Workspace.resolve(path)))
        except Exception as e:
            return str(e)

    @staticmethod
    def grep_file(path: str, pattern: str) -> str:
        try:
            content = Workspace.resolve(path).read_text()
            matches = [line for line in content.split('\n') if re.search(pattern, line)]
            return "\n".join(matches) if matches else "NO MATCHES"
        except Exception as e:
            return str(e)

    @staticmethod
    def search_memory(query: str) -> str:
        return "\n".join(memory.search(query))

    @staticmethod
    def remember(key: str, value: str) -> str:
        memory.add(key, value)
        return "REMEMBERED"

    @staticmethod
    def ask_user(question: str) -> str:
        return input(f"[AGENT] {question}: ")

    @classmethod
    def get_schema(cls):
        return [
            {"name": "read_file", "description": "Read file content", "parameters": {"path": "string"}},
            {"name": "write_file", "description": "Write content to file", "parameters": {"path": "string", "content": "string"}},
            {"name": "list_directory", "description": "List directory contents", "parameters": {"path": "string"}},
            {"name": "grep_file", "description": "Search for pattern in file", "parameters": {"path": "string", "pattern": "string"}},
            {"name": "search_memory", "description": "Search agent memory", "parameters": {"query": "string"}},
            {"name": "remember", "description": "Store information in memory", "parameters": {"key": "string", "value": "string"}},
            {"name": "ask_user", "description": "Ask user a question", "parameters": {"question": "string"}},
        ]
