/* statusLine shim：把 CC 从 stdin 给的 JSON 转给本地 daemon，原样吐回渲染结果。
 *
 * 编译（务必 -mwindows：GUI 子系统 → 不分配控制台 → 不产生 conhost 进程）：
 *     gcc -O2 -mwindows -o dspg-status.exe dspg-status.c -lws2_32
 *
 * 兜底链（按顺序）：
 *   1. 连上 daemon → 正常渲染（约 15 ms）
 *   2. 连不上 → 顺手把 daemon 拉起来（pythonw，无窗口），并打印
 *      %USERPROFILE%\.claude\.dspg_line.txt 里 daemon 上一帧写的整行
 *   3. 连兜底文件都没有 → 打印一句提示，不阻塞 CC
 * 连接用非阻塞 + 400 ms 超时：daemon 不在时绝不把这一帧拖成两秒（那正是雪崩起点）。
 */
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

#define PORT 18799

/* GUI 子系统（-mwindows）下 CRT 的 stdout 常常接不上继承来的管道句柄，
   printf/fputs 会**静默丢弃**输出（实测：整行渲染好了但屏幕上是空的）。
   所以标准流一律用 ReadFile/WriteFile 直接走继承句柄。 */
static void write_out(const char *s, size_t n) {
    HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD w = 0;
    if (h && h != INVALID_HANDLE_VALUE && n) WriteFile(h, s, (DWORD)n, &w, NULL);
}

static size_t read_all_stdin(char *buf, size_t cap) {
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    size_t total = 0;
    if (!h || h == INVALID_HANDLE_VALUE) return 0;
    while (total < cap - 1) {
        DWORD r = 0;
        if (!ReadFile(h, buf + total, (DWORD)(cap - 1 - total), &r, NULL) || r == 0) break;
        total += r;
    }
    buf[total] = 0;
    return total;
}

static void emit_fallback(void) {
    char path[MAX_PATH], buf[8192], head[64];
    int n;
    const char *home = getenv("USERPROFILE");
    if (!home) return;
    snprintf(path, sizeof(path), "%s\\.claude\\.dspg_line.txt", home);
    FILE *f = fopen(path, "rb");
    if (!f) {
        n = snprintf(head, sizeof(head),
                     "\xe6\xa3\x80\xe6\x9f\xa5\xe5\x99\xa8\xe5\x90\xaf\xe5\x8a\xa8\xe4\xb8\xad\xe2\x80\xa6\n");
        write_out(head, (size_t)n);   /* 「检查器启动中…」（UTF-8） */
        return;
    }
    n = (int)fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    if (n > 0) write_out(buf, (size_t)n);
}

/* 拉起 daemon：路径全部就地推导，不写死任何本机路径。
   pythonw 的找法（依次）：环境变量 DSPG_PYTHONW → 与 shim 同目录的 dspg.env 里
   一行 `PYTHONW=<绝对路径>` → 交给 CreateProcess 在 PATH 里找 pythonw.exe。 */
static void start_daemon(void) {
    wchar_t self[MAX_PATH], dir[MAX_PATH], cmd[2048], buf[2048];
    wchar_t *slash;
    const wchar_t *pyw = L"pythonw.exe";
    DWORD n;
    FILE *f;

    n = GetModuleFileNameW(NULL, self, MAX_PATH);
    if (!n) return;
    wcscpy(dir, self);
    slash = wcsrchr(dir, L'\\');
    if (!slash) return;
    *slash = 0;                               /* dir = shim 所在目录 */

    if (GetEnvironmentVariableW(L"DSPG_PYTHONW", buf, 2048) > 0) {
        pyw = buf;                            /* 必须拷到静态生存期：buf 在栈上，够用 */
    } else {
        wchar_t env_path[MAX_PATH + 16];
        _snwprintf(env_path, MAX_PATH + 16, L"%ls\\dspg.env", dir);
        f = _wfopen(env_path, L"rb");
        if (f) {
            static wchar_t cfg[2048];
            char line[1024];
            while (fgets(line, sizeof(line), f)) {
                if (strncmp(line, "PYTHONW=", 8) == 0) {
                    char *v = line + 8, *e = v + strlen(v);
                    while (e > v && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ')) *--e = 0;
                    MultiByteToWideChar(CP_UTF8, 0, v, -1, cfg, 2048);
                    pyw = cfg;
                    break;
                }
            }
            fclose(f);
        }
    }
    /* ⚠️ 必须走 `cmd /c start` 而不是直接 CreateProcess：
       CC 会在状态栏命令结束后 taskkill /T 清进程树，如果 daemon 是我们的**直接子进程**，
       它会被一起端掉（实测 daemon 被反复重启）。让 cmd 当爹并立刻退出 → daemon 变孤儿，
       脱离任何进程树，CC 就够不着它了。 */
    _snwprintf(cmd, 2048, L"cmd /c start \"\" /b \"%ls\" \"%ls\\statusd.py\"", pyw, dir);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (CreateProcessW(NULL, cmd, NULL, NULL, FALSE,
                       CREATE_NO_WINDOW | DETACHED_PROCESS, NULL, NULL, &si, &pi)) {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
}

int main(void) {
    static char inbuf[1 << 16];
    static char outbuf[1 << 18];
    WSADATA w;
    size_t n;
    SOCKET s;
    struct sockaddr_in a;
    fd_set wf;
    struct timeval tv;
    u_long nb = 1;
    int total = 0, r;

    if (WSAStartup(MAKEWORD(2, 2), &w) != 0) {
        emit_fallback();
        return 0;
    }
    n = read_all_stdin(inbuf, sizeof(inbuf));

    s = socket(AF_INET, SOCK_STREAM, 0);
    if (s == INVALID_SOCKET) {
        WSACleanup();
        emit_fallback();
        return 0;
    }
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(PORT);
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);

    ioctlsocket(s, FIONBIO, &nb);           /* 非阻塞连接 + 400ms 超时 */
    connect(s, (struct sockaddr *)&a, sizeof(a));
    FD_ZERO(&wf);
    FD_SET(s, &wf);
    tv.tv_sec = 0;
    tv.tv_usec = 400000;
    if (select(0, NULL, &wf, NULL, &tv) <= 0) {
        closesocket(s);
        WSACleanup();
        start_daemon();                     /* 顺手拉起 daemon（下一次刷新就能用上） */
        emit_fallback();
        return 0;
    }
    nb = 0;
    ioctlsocket(s, FIONBIO, &nb);           /* 转回阻塞收发 */

    if (n) send(s, inbuf, (int)n, 0);
    shutdown(s, SD_SEND);
    while ((r = recv(s, outbuf + total, (int)(sizeof(outbuf) - 1 - total), 0)) > 0) {
        total += r;
        if ((size_t)total >= sizeof(outbuf) - 1) break;
    }
    outbuf[total] = 0;
    if (total > 0) {
        write_out(outbuf, (size_t)total);
        if (outbuf[total - 1] != '\n') write_out("\n", 1);
    } else {
        emit_fallback();
    }
    closesocket(s);
    WSACleanup();
    return 0;
}
