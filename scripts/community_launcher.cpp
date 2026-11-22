#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>

#include <filesystem>
#include <fstream>
#include <string>

void write_test_launch_failure(const std::filesystem::path& root, DWORD error) {
    std::error_code fs_error;
    if (!std::filesystem::is_regular_file(root / L"persona_runtime" / L"community-field-test", fs_error)) {
        return;
    }
    wchar_t local[32768];
    const DWORD size = GetEnvironmentVariableW(L"LOCALAPPDATA", local, ARRAYSIZE(local));
    if (size == 0 || size >= ARRAYSIZE(local)) {
        return;
    }
    const auto folder = std::filesystem::path(local) / L"PCAR Community Test Reports";
    std::filesystem::create_directories(folder, fs_error);
    if (fs_error) {
        return;
    }
    const auto report = folder / (L"PCAR-启动失败-" + std::to_wstring(GetCurrentProcessId()) +
                                  L"-" + std::to_wstring(GetTickCount64()) + L".md");
    std::ofstream stream(report, std::ios::binary);
    stream << "# PCAR 社区测试版验收日志\n\n"
           << "- 中断位置：启动随包 Python。\n"
           << "- Windows 错误代码：" << error << "\n";
    stream.close();
    if (stream) {
        ShellExecuteW(nullptr, L"open", folder.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
    }
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    wchar_t module[32768];
    const DWORD length = GetModuleFileNameW(nullptr, module, ARRAYSIZE(module));
    if (length == 0 || length >= ARRAYSIZE(module)) {
        return 1;
    }
    const auto root = std::filesystem::path(std::wstring(module, length)).parent_path();
    const auto python = root / L"python" / L"pythonw.exe";
    std::wstring command = L"\"" + python.wstring() +
        L"\" -E -s -B -m persona_runtime.community_launch";
    SetEnvironmentVariableW(L"PCAR_EDITION", L"community");
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(python.c_str(), command.data(), nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, root.c_str(), &startup, &process)) {
        const DWORD error = GetLastError();
        write_test_launch_failure(root, error);
        MessageBoxW(nullptr, L"无法启动随包附带的 Python。请重新安装 PCAR 社区版。",
                    L"PCAR 社区版", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
