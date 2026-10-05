package agent

allowed_base_commands := {
    "apt", "apt-get", "bash", "sh", "curl", "wget", "git", "python3",
    "pip", "pip3", "npm", "node", "nmap", "nuclei", "semgrep", "trufflehog",
    "ffuf", "gobuster", "sqlmap", "ls", "cat", "head", "tail", "grep",
    "find", "wc", "sort", "uniq", "echo", "date", "whoami", "pwd",
    "mkdir", "touch", "cp", "mv", "rm", "ollama", "opa", "proot-distro"
}

blocked_patterns := [
    "rm -rf /", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:", "chmod 777 /",
    "> /dev/sda"
]

base_command(cmd) := parts[0] if {
    parts := split(trim_space(cmd), " ")
    count(parts) > 0
}

# فحص الأنابيب الخطيرة: يلتقط "curl ... | sh" و "wget ... | bash"
dangerous_pipe(cmd) if {
    contains(cmd, "curl")
    contains(cmd, "|")
    contains(cmd, "sh")
}
dangerous_pipe(cmd) if {
    contains(cmd, "wget")
    contains(cmd, "|")
    contains(cmd, "sh")
}
dangerous_pipe(cmd) if {
    contains(cmd, "curl")
    contains(cmd, "|")
    contains(cmd, "bash")
}
dangerous_pipe(cmd) if {
    contains(cmd, "wget")
    contains(cmd, "|")
    contains(cmd, "bash")
}

touches_critical_path(cmd) if {
    critical_paths := ["/etc/passwd", "/etc/shadow", "/root/.ssh", "/var/run/docker.sock"]
    path := critical_paths[_]
    contains(cmd, path)
}

default allow := false

allow if {
    cmd := input.command
    base := base_command(cmd)
    allowed_base_commands[base]
    not dangerous_pipe(cmd)
    not touches_critical_path(cmd)
}
