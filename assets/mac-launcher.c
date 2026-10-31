#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

int main(void) {
    char executable[PATH_MAX], resolved[PATH_MAX], command[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0 || !realpath(executable, resolved)) return 1;
    char *slash = strrchr(resolved, '/');
    if (!slash) return 1;
    *slash = '\0';
    const char *script = strstr(resolved, "/安装 CutCut.app/") ? "install.command" : "start.command";
    int n = snprintf(command, sizeof(command), "%s/../Resources/%s", resolved, script);
    if (n < 0 || (size_t)n >= sizeof(command) || access(command, R_OK) != 0) {
        execl("/usr/bin/osascript", "osascript", "-e", "display alert \"无法找到 CutCut 文件\" message \"请保留完整的应用及项目文件夹。\"", (char *)NULL);
        return 1;
    }
    if (strcmp(script, "start.command") == 0) {
        char root[PATH_MAX], python[PATH_MAX], helper[PATH_MAX];
        if (snprintf(root, sizeof(root), "%s/../../..", resolved) >= (int)sizeof(root) ||
            snprintf(python, sizeof(python), "%s/.venv/bin/python", root) >= (int)sizeof(python) ||
            snprintf(helper, sizeof(helper), "%s/instance_control.py", root) >= (int)sizeof(helper)) return 1;
        if (access(python, X_OK) == 0) {
            pid_t child = fork();
            if (child == 0) { execl(python, python, helper, "--launch", (char *)NULL); _exit(1); }
            if (child > 0) {
                int status;
                if (waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0) return 0;
            }
        }
    }
    execl("/usr/bin/open", "open", "-a", "Terminal", command, (char *)NULL);
    return 1;
}
