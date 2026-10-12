// Streaming OpenSSH DefaultShell adapter. No commands, output, or environment
// are logged. The shell and its DLLs come from an owner-protected private Git
// runtime beside this executable, never from the shared C:\Utils installation.
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

int main(void) {
    wchar_t module[32768], shell[32768], args[32768];
    DWORD n = GetModuleFileNameW(NULL, module, 32768);
    if (!n || n >= 32768) return 127;
    wchar_t* slash = wcsrchr(module, L'\\');
    if (!slash) return 127;
    *slash = 0;
    if (_snwprintf_s(shell, 32768, _TRUNCATE, L"%s\\runtime\\Git\\bin\\sh.exe", module) < 0)
        return 127;

    // Prefer the same protected runtime for shell probes and child utilities.
    wchar_t oldpath[32768], newpath[32768];
    oldpath[0] = 0;
    DWORD pathlen = GetEnvironmentVariableW(L"PATH", oldpath, 32768);
    if (pathlen >= 32768) return 127;
    if (_snwprintf_s(newpath, 32768, _TRUNCATE,
        L"%s\\runtime\\Git\\cmd;%s\\runtime\\Git\\usr\\bin;%s\\runtime\\Git\\mingw64\\bin;%s",
        module, module, module, oldpath) < 0) return 127;
    if (!SetEnvironmentVariableW(L"PATH", newpath)) return 127;

    wchar_t* p = GetCommandLineW();
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') p++;
        if (*p) p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t') p++;
    }
    while (*p == L' ' || *p == L'\t') p++;
    if (p[0] == L'/' && (p[1] == L'c' || p[1] == L'C') &&
        (p[2] == L' ' || p[2] == L'\t' || p[2] == 0)) p[0] = L'-';
    if (_snwprintf_s(args, 32768, _TRUNCATE, L"\"%s\" %s", shell, p) < 0)
        return 127;

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    ZeroMemory(&pi, sizeof pi);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (!CreateProcessW(shell, args, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fprintf(stderr, "Herdr SSH shell unavailable (%lu)\n", GetLastError());
        return 127;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 127;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
