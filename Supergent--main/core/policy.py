class Policy:
    CAPABILITIES = {
        "read_file": True,
        "write_file": True,
        "list_directory": True,
        "grep_file": True,
        "search_memory": True,
        "remember": True,
        "ask_user": True,
        "run_python": False
    }

    FORBIDDEN_TASKS = ["rm -rf", "format", "shutdown", "mkfs", "dd if=", "> /dev/"]

    @classmethod
    def allow(cls, tool: str) -> bool:
        return cls.CAPABILITIES.get(tool, False)

    @classmethod
    def check_task(cls, task: str) -> bool:
        return not any(x in task.lower() for x in cls.FORBIDDEN_TASKS)
