#requires -Version 5.1
<#
.SYNOPSIS
    Safely stops the installed MoteDeck.exe before upgrade, uninstall,
    or an explicit user-requested stop.

.DESCRIPTION
    Current builds receive an atomic full-exit request and own all cleanup.
    Released legacy builds have no application-exit contract, so their bridge
    is asked to exit through its existing tray control window. Any remaining
    settings shell is brought forward for the user to save or discard changes
    and exit. During installation, a same-named program running from another
    folder is also brought forward and blocks the install, preventing a legacy
    portable copy from intercepting the newly installed launch. Such external
    programs are never force-stopped.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$AppPath,

    [string]$ConfigRoot = "",

    [switch]$BlockOtherLocations,

    [switch]$ElevatedRetry
)

$ErrorActionPreference = "Stop"

# 兜底：脚本自身异常（安全软件拦截、组策略限制、WMI 不可用、Add-Type 被
# 禁止等）不能让进程以 1 之类的模糊退出码结束，否则安装程序只能笼统地报
# “没有完全退出”。统一转成 exitScriptFailed，并留下可读的错误信息。
trap {
    # trap 自身也不能再抛错，否则 PowerShell 会以 1 退出，安装程序又只能
    # 笼统地报“没有完全退出”。
    try {
        $trapMessage = "stop-app.ps1 failed: " + $_.Exception.GetType().FullName +
            ": " + $_.Exception.Message
        [Console]::Error.WriteLine($trapMessage)
        try {
            $trapLogRoot = [System.IO.Path]::Combine(
                [Environment]::GetFolderPath(
                    [Environment+SpecialFolder]::LocalApplicationData
                ),
                "MoteDeck",
                "logs"
            )
            [void][System.IO.Directory]::CreateDirectory($trapLogRoot)
            [System.IO.File]::AppendAllText(
                [System.IO.Path]::Combine($trapLogRoot, "installer-stop.log"),
                ("{0} {1}{2}" -f [DateTime]::UtcNow.ToString("o"), $trapMessage,
                    [Environment]::NewLine)
            )
        } catch {
            # 日志写入本身失败时保持静默，退出码仍然是脚本异常码。
            # 显式语句：即使文件编码判断失误，空 catch 块也不会被
            # PowerShell 判成非法结构（EmptyCatchNotLast），保证仍能退出 30。
            $null = 1
        }
    } catch {
        # 诊断信息也拿不到时什么都不做，只保证退出码。
        $null = 1
    }
    exit 30
}

$exitOk = 0
$exitNeedsElevation = 10
$exitUnsafeToContinue = 20
$exitProbeFailed = 21
$exitUserActionRequired = 22
$exitOtherLocationRunning = 23
$exitOtherSessionRunning = 24
# 脚本自身异常（安全软件拦截、组策略禁用脚本、Add-Type 被禁止等）统一用
# 30 退出，让安装程序能把“清理脚本崩了”和“旧版仍在运行”区分开。
$exitScriptFailed = 30
$applicationExitRejectedExitCode = 21
$maintenanceExitTimeoutSeconds = 50
$legacyBridgeExitTimeoutSeconds = 10
# 主程序命名经历过三代：现行固定 MoteDeck.exe、文件名冻结前的
# "MoteDeck <版本>.exe"、最初的 RemoteMicRC003.exe。停止逻辑必须
# 三代都识别，否则升级时找不到正在运行的旧版进程，旧进程会存活到
# 安装结束并留下无法清理的隔离目录。
$targetExecutableNames = @("MoteDeck.exe", "RemoteMicRC003.exe")
$targetVersionedExecutableNamePattern = "^MoteDeck .+\.exe$"
$targetProcessNameFilter =
    "Name = 'MoteDeck.exe' OR Name = 'RemoteMicRC003.exe' OR Name LIKE 'MoteDeck %.exe'"
$v3ExitCapabilityProperty = "RemoteMicRC003.ApplicationExitRequestV3"
$windowExitCapabilityProperty = "RemoteMicRC003.ApplicationExitWindowSignalV1"
$windowExitRequestProperty = "RemoteMicRC003.ApplicationExitWindowRequestV1"
$windowExitRejectedProperty = "RemoteMicRC003.ApplicationExitWindowRejectedV1"
$applicationExitRequestMutexName = "Local\RemoteMicRC003_ApplicationExitRequest"
# 与 single_instance.py 中的常量保持一致；用于在进程身份读不到时判断
# “到底有没有实例在运行”。
$settingsInstanceMutexName = "Local\RemoteMicRC003_SettingsInstance"
$bridgeInstanceMutexName = "Local\RemoteMicRC003_BridgeInstance"
$handoffMutexName = "Local\RemoteMicRC003_ApplicationHandoff"
$legacyBridgeWindowTitle = "Remote Mic RC003 bridge tray"
$legacyExitCommand = 1002
$wmCommand = 0x0111
$errorAccessDenied = 5
$productName = -join @(
    [char]0x65E0,
    [char]0x7EBF,
    [char]0x9EA6
)

function Write-StopError {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Initialize-WindowMethods {
    $nativeType = [System.Management.Automation.PSTypeName]'RemoteMicInstaller.LegacyShellMethods'
    if (-not $nativeType.Type) {
        Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace RemoteMicInstaller {
    public enum WindowPropertyCleanupResult {
        NoAction = 0,
        RemovedOwn = 1,
        RestoredOther = 2,
        Failed = 3
    }

    public static class LegacyShellMethods {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool ShowWindow(IntPtr window, int command);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool BringWindowToTop(IntPtr window);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetForegroundWindow(IntPtr window);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool FlashWindow(IntPtr window, bool invert);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowTextLength(IntPtr window);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(
            IntPtr window,
            System.Text.StringBuilder text,
            int maximumCount
        );

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr GetProp(IntPtr window, string propertyName);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetProp(
            IntPtr window,
            string propertyName,
            IntPtr value
        );

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr RemoveProp(
            IntPtr window,
            string propertyName
        );

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool PostMessage(
            IntPtr window,
            uint message,
            UIntPtr wParam,
            IntPtr lParam
        );

        public static IntPtr FindWindowForProcess(uint processId) {
            IntPtr found = IntPtr.Zero;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                uint ownerProcessId;
                GetWindowThreadProcessId(window, out ownerProcessId);
                if (ownerProcessId != processId || GetWindowTextLength(window) <= 0) {
                    return true;
                }
                found = window;
                return false;
            }, IntPtr.Zero);
            return found;
        }

        public static IntPtr FindWindowForProcessAndTitle(
            uint processId,
            string expectedTitle
        ) {
            IntPtr found = IntPtr.Zero;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                uint ownerProcessId;
                GetWindowThreadProcessId(window, out ownerProcessId);
                int length = GetWindowTextLength(window);
                if (ownerProcessId != processId || length <= 0) {
                    return true;
                }
                var text = new System.Text.StringBuilder(length + 1);
                GetWindowText(window, text, text.Capacity);
                if (!String.Equals(text.ToString(), expectedTitle, StringComparison.Ordinal)) {
                    return true;
                }
                found = window;
                return false;
            }, IntPtr.Zero);
            return found;
        }

        public static bool ProcessWindowHasProperty(
            uint processId,
            string propertyName
        ) {
            bool found = false;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                uint ownerProcessId;
                GetWindowThreadProcessId(window, out ownerProcessId);
                if (
                    ownerProcessId == processId &&
                    GetProp(window, propertyName) != IntPtr.Zero
                ) {
                    found = true;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return found;
        }

        public static bool TrySetProcessWindowProperty(
            uint processId,
            string requiredPropertyName,
            string requestPropertyName,
            IntPtr requestValue,
            out bool matchingWindowFound,
            out bool requestAlreadyPresent,
            out int error
        ) {
            bool localMatchingWindowFound = false;
            bool localRequestAlreadyPresent = false;
            bool requested = false;
            int localError = 0;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                uint ownerProcessId;
                GetWindowThreadProcessId(window, out ownerProcessId);
                if (
                    ownerProcessId != processId ||
                    GetProp(window, requiredPropertyName) == IntPtr.Zero
                ) {
                    return true;
                }
                localMatchingWindowFound = true;
                if (GetProp(window, requestPropertyName) != IntPtr.Zero) {
                    localRequestAlreadyPresent = true;
                    return false;
                }
                requested = SetProp(
                    window,
                    requestPropertyName,
                    requestValue
                );
                if (!requested) {
                    localError = Marshal.GetLastWin32Error();
                }
                return false;
            }, IntPtr.Zero);
            matchingWindowFound = localMatchingWindowFound;
            requestAlreadyPresent = localRequestAlreadyPresent;
            error = localError;
            return requested;
        }

        public static IntPtr GetProcessWindowPropertyValue(
            uint processId,
            string requiredPropertyName,
            string propertyName
        ) {
            IntPtr value = IntPtr.Zero;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                uint ownerProcessId;
                GetWindowThreadProcessId(window, out ownerProcessId);
                if (
                    ownerProcessId != processId ||
                    GetProp(window, requiredPropertyName) == IntPtr.Zero
                ) {
                    return true;
                }
                value = GetProp(window, propertyName);
                return false;
            }, IntPtr.Zero);
            return value;
        }

        public static WindowPropertyCleanupResult TryRemoveProcessWindowPropertyValue(
            uint processId,
            string requiredPropertyName,
            string propertyName,
            IntPtr expectedValue,
            out int error
        ) {
            WindowPropertyCleanupResult result = WindowPropertyCleanupResult.NoAction;
            int localError = 0;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                uint ownerProcessId;
                GetWindowThreadProcessId(window, out ownerProcessId);
                if (
                    ownerProcessId != processId ||
                    GetProp(window, requiredPropertyName) == IntPtr.Zero
                ) {
                    return true;
                }
                if (GetProp(window, propertyName) != expectedValue) {
                    return false;
                }
                IntPtr removedValue = RemoveProp(window, propertyName);
                if (removedValue == expectedValue) {
                    result = WindowPropertyCleanupResult.RemovedOwn;
                    return false;
                }
                if (removedValue == IntPtr.Zero) {
                    localError = Marshal.GetLastWin32Error();
                    if (GetProp(window, propertyName) == expectedValue) {
                        result = WindowPropertyCleanupResult.Failed;
                    }
                    return false;
                }

                IntPtr replacementValue = GetProp(window, propertyName);
                if (replacementValue == removedValue) {
                    result = WindowPropertyCleanupResult.RestoredOther;
                    return false;
                }
                if (replacementValue != IntPtr.Zero) {
                    result = WindowPropertyCleanupResult.Failed;
                    return false;
                }
                if (SetProp(window, propertyName, removedValue)) {
                    result = WindowPropertyCleanupResult.RestoredOther;
                } else {
                    localError = Marshal.GetLastWin32Error();
                    result = WindowPropertyCleanupResult.Failed;
                }
                return false;
            }, IntPtr.Zero);
            error = localError;
            return result;
        }
    }
}
"@
    }
}

function Show-LegacyShellWindow {
    param([Parameter(Mandatory = $true)][object[]]$Targets)

    Initialize-WindowMethods

    foreach ($target in $Targets) {
        try {
            $window = [RemoteMicInstaller.LegacyShellMethods]::FindWindowForProcess(
                [uint32]$target.ProcessId
            )
            if ($window -eq [IntPtr]::Zero) {
                continue
            }
            [void][RemoteMicInstaller.LegacyShellMethods]::ShowWindow($window, 9)
            [void][RemoteMicInstaller.LegacyShellMethods]::BringWindowToTop($window)
            if (-not [RemoteMicInstaller.LegacyShellMethods]::SetForegroundWindow($window)) {
                [void][RemoteMicInstaller.LegacyShellMethods]::FlashWindow($window, $true)
            }
        } catch {
            # Best effort only. The installer message also names the tray exit path.
            $null = 1
        }
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

$isElevated = Test-IsAdministrator
$currentSessionId = [uint32][System.Diagnostics.Process]::GetCurrentProcess().SessionId
if ($ElevatedRetry -and -not $isElevated) {
    Write-StopError "$productName elevated stop retry did not receive administrator rights."
    exit $exitProbeFailed
}

try {
    $root = [System.IO.Path]::GetFullPath($AppPath).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
} catch {
    Write-StopError "$productName application path could not be normalized."
    exit $exitProbeFailed
}

if ([string]::IsNullOrWhiteSpace($ConfigRoot)) {
    $localAppData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    $ConfigRoot = [System.IO.Path]::Combine($localAppData, "MoteDeck")
}
function Exit-ForRestrictedProcess {
    if (-not $script:isElevated) {
        exit $script:exitNeedsElevation
    }
    Write-StopError "$productName process identity could not be verified."
    exit $script:exitProbeFailed
}

# 进程身份读不到（以管理员身份运行的子进程、被保护进程等）时，不能仅凭
# “读不到”就阻断安装：安装包里可能根本没有旧版在运行，用户会被迫重启
# Windows 才能升级。程序自身持有的命名互斥体才是“确实有实例在运行”的
# 权威证据，所以先查互斥体，只有确认有实例在运行才按受限处理。
function Test-InstanceMutexRunning {
    $names = @(
        $script:settingsInstanceMutexName,
        $script:bridgeInstanceMutexName,
        $script:handoffMutexName
    )
    $inconclusive = $false
    foreach ($name in $names) {
        $handle = $null
        try {
            $handle = [System.Threading.Mutex]::OpenExisting($name)
            return $true
        } catch [System.Threading.WaitHandleCannotBeOpenedException] {
            continue
        } catch {
            # 无法判断时必须保守：仍按“可能在运行”处理，绝不误覆盖文件。
            $inconclusive = $true
        } finally {
            if ($null -ne $handle) {
                try {
                    $handle.Dispose()
                } catch {
                    $inconclusive = $true
                }
            }
        }
    }
    if ($inconclusive) {
        return $null
    }
    return $false
}

function Exit-ForUnverifiedProcess {
    # 身份读不到，但没有任何实例互斥体：说明当前桌面没有正在使用的实例
    # （典型情况是提权运行的语音子进程，它不持有实例互斥体），安装可以
    # 继续。互斥体查不出来时才退回受限处理，绝不冒险覆盖正在使用的文件。
    $instanceRunning = Test-InstanceMutexRunning
    if ($instanceRunning -eq $false) {
        Write-StopError (
            "$productName process identity was unavailable but no running " +
            "instance owns the application mutex; continuing."
        )
        exit $script:exitOk
    }
    Exit-ForRestrictedProcess
}

function Get-TargetSnapshot {
    $targets = [System.Collections.Generic.List[object]]::new()
    $otherSessionTargets = [System.Collections.Generic.List[object]]::new()
    $unknownCount = 0
    try {
        $processes = @(
            Get-CimInstance Win32_Process `
                -Filter $script:targetProcessNameFilter `
                -ErrorAction Stop
        )
    } catch {
        Write-StopError "$productName process list could not be read."
        exit $script:exitProbeFailed
    }

    foreach ($process in $processes) {
        if (
            -not $process.ExecutablePath -or
            -not $process.CreationDate -or
            $null -eq $process.SessionId
        ) {
            $unknownCount += 1
            continue
        }
        try {
            $executable = [System.IO.Path]::GetFullPath($process.ExecutablePath)
        } catch {
            $unknownCount += 1
            continue
        }
        $executableName = [System.IO.Path]::GetFileName($executable)
        $matchesTargetName = (
            $script:targetExecutableNames -contains $executableName -or
            $executableName -match $script:targetVersionedExecutableNamePattern
        )
        $matchesTargetPath = (
            $matchesTargetName -and
            [System.IO.Path]::GetDirectoryName($executable).Equals(
                $script:root,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )
        $target = [PSCustomObject]@{
            ProcessId = [uint32]$process.ProcessId
            CreationDate = $process.CreationDate
            CommandLine = [string]$process.CommandLine
            SessionId = [uint32]$process.SessionId
            ExecutablePath = $executable
            ExternalLocation = -not $matchesTargetPath
        }
        if ([uint32]$process.SessionId -ne $script:currentSessionId) {
            if ($matchesTargetPath) {
                $otherSessionTargets.Add($target)
            }
            continue
        }
        if (-not $matchesTargetPath) {
            if ($script:BlockOtherLocations) {
                $targets.Add($target)
            }
            continue
        }
        $targets.Add($target)
    }

    if ($otherSessionTargets.Count -gt 0) {
        Write-StopError "$productName is running in another Windows session."
        exit $script:exitOtherSessionRunning
    }

    return [PSCustomObject]@{
        Targets = @($targets)
        UnknownCount = $unknownCount
    }
}

function Get-CurrentTargetProcess {
    param(
        [Parameter(Mandatory = $true)]
        [uint32]$ProcessId,

        [Parameter(Mandatory = $true)]
        $CreationDate,

        [Parameter(Mandatory = $true)]
        [uint32]$SessionId,

        [Parameter(Mandatory = $true)]
        [string]$ExecutablePath
    )

    $current = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $ProcessId" `
        -ErrorAction SilentlyContinue
    if (-not $current -or $current.CreationDate -ne $CreationDate) {
        return $null
    }
    if ([uint32]$current.SessionId -ne $SessionId) {
        return $null
    }
    if (-not $current.ExecutablePath) {
        throw "target process path is unavailable"
    }
    $executable = [System.IO.Path]::GetFullPath($current.ExecutablePath)
    if (-not $executable.Equals(
        $ExecutablePath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $null
    }
    return $current
}

function Wait-ForTargetsToExit {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Targets,

        [Parameter(Mandatory = $true)]
        [double]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $remaining = @()
        foreach ($target in $Targets) {
            try {
                $current = Get-CurrentTargetProcess `
                    -ProcessId $target.ProcessId `
                    -CreationDate $target.CreationDate `
                    -SessionId $target.SessionId `
                    -ExecutablePath $target.ExecutablePath
            } catch {
                Exit-ForRestrictedProcess
            }
            if ($current) {
                $remaining += $target
            }
        }
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Test-WindowCapability {
    param(
        [Parameter(Mandatory = $true)][object]$Target,
        [Parameter(Mandatory = $true)][string]$PropertyName
    )

    Initialize-WindowMethods
    return [RemoteMicInstaller.LegacyShellMethods]::ProcessWindowHasProperty(
        [uint32]$Target.ProcessId,
        $PropertyName
    )
}

function Enter-ApplicationExitRequestMutex {
    $mutex = $null
    try {
        $createdNew = $false
        $mutex = [System.Threading.Mutex]::new(
            $false,
            $script:applicationExitRequestMutexName,
            [ref]$createdNew
        )
        $acquired = $false
        try {
            $acquired = $mutex.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        return [PSCustomObject]@{
            Mutex = $mutex
            Acquired = [bool]$acquired
            Available = $true
        }
    } catch {
        if ($null -ne $mutex) {
            try {
                $mutex.Dispose()
            } catch {
                # The caller will fail closed because the mutex is unavailable.
                $null = 1
            }
        }
        return [PSCustomObject]@{
            Mutex = $null
            Acquired = $false
            Available = $false
        }
    }
}

function Exit-ApplicationExitRequestMutex {
    param([Parameter(Mandatory = $true)][object]$Lease)

    $complete = $true
    if ($Lease.Acquired) {
        try {
            $Lease.Mutex.ReleaseMutex()
        } catch {
            $complete = $false
        }
    }
    if ($null -ne $Lease.Mutex) {
        try {
            $Lease.Mutex.Dispose()
        } catch {
            $complete = $false
        }
    }
    return $complete
}

function Invoke-WindowFullExit {
    param([Parameter(Mandatory = $true)][object]$Target)

    Initialize-WindowMethods
    $lease = Enter-ApplicationExitRequestMutex
    if (-not $lease.Available) {
        return "failed"
    }
    if (-not $lease.Acquired) {
        $released = Exit-ApplicationExitRequestMutex -Lease $lease
        if (-not $released) {
            return "failed"
        }
        if (Wait-ForTargetsToExit -Targets @($Target) -TimeoutSeconds $script:maintenanceExitTimeoutSeconds) {
            return "exited"
        }
        return "busy"
    }

    $requestToken = Get-Random -Minimum 1 -Maximum ([int]::MaxValue)
    $matchingWindowFound = $false
    $requestAlreadyPresent = $false
    $requestError = 0
    $requestOwned = $false
    $needsElevation = $false
    $result = "failed"
    try {
        $requested = [RemoteMicInstaller.LegacyShellMethods]::TrySetProcessWindowProperty(
            [uint32]$Target.ProcessId,
            $script:windowExitCapabilityProperty,
            $script:windowExitRequestProperty,
            [IntPtr]$requestToken,
            [ref]$matchingWindowFound,
            [ref]$requestAlreadyPresent,
            [ref]$requestError
        )
        if (-not $requested) {
            if (
                $matchingWindowFound -and
                $requestError -eq $script:errorAccessDenied -and
                -not $script:isElevated
            ) {
                $needsElevation = $true
            } elseif ($requestAlreadyPresent) {
                if (Wait-ForTargetsToExit -Targets @($Target) -TimeoutSeconds $script:maintenanceExitTimeoutSeconds) {
                    $result = "exited"
                } else {
                    $result = "busy"
                }
            }
        } else {
            $requestOwned = $true
            $deadline = [DateTime]::UtcNow.AddSeconds(
                $script:maintenanceExitTimeoutSeconds
            )
            do {
                try {
                    $current = Get-CurrentTargetProcess `
                        -ProcessId $Target.ProcessId `
                        -CreationDate $Target.CreationDate `
                        -SessionId $Target.SessionId `
                        -ExecutablePath $Target.ExecutablePath
                } catch {
                    Exit-ForRestrictedProcess
                }
                if (-not $current) {
                    $result = "exited"
                    break
                }
                $rejected = [RemoteMicInstaller.LegacyShellMethods]::GetProcessWindowPropertyValue(
                    [uint32]$Target.ProcessId,
                    $script:windowExitCapabilityProperty,
                    $script:windowExitRejectedProperty
                )
                if ($rejected.ToInt64() -eq [int64]$requestToken) {
                    $result = "rejected"
                    break
                }
                Start-Sleep -Milliseconds 100
            } while ([DateTime]::UtcNow -lt $deadline)
            if ($result -eq "failed") {
                $result = "timeout"
            }
        }
    } catch {
        $result = "failed"
    } finally {
        if ($requestOwned) {
            try {
                $cleanupError = 0
                $cleanupResult = [RemoteMicInstaller.LegacyShellMethods]::TryRemoveProcessWindowPropertyValue(
                    [uint32]$Target.ProcessId,
                    $script:windowExitCapabilityProperty,
                    $script:windowExitRequestProperty,
                    [IntPtr]$requestToken,
                    [ref]$cleanupError
                )
                if ($cleanupResult -eq [RemoteMicInstaller.WindowPropertyCleanupResult]::Failed) {
                    $result = "failed"
                }
            } catch {
                $result = "failed"
            }
        }
        if (-not (Exit-ApplicationExitRequestMutex -Lease $lease)) {
            $result = "failed"
        }
    }
    if ($needsElevation) {
        exit $script:exitNeedsElevation
    }
    return $result
}

function Invoke-LegacyV3FullExit {
    param([Parameter(Mandatory = $true)][object]$Target)

    # An elevated cleanup process must never execute a program from a
    # user-writable installation or portable directory.
    if ($script:isElevated) {
        return $false
    }

    try {
        $requestProcess = Start-Process `
            -FilePath $Target.ExecutablePath `
            -ArgumentList "--request-exit" `
            -WorkingDirectory ([System.IO.Path]::GetDirectoryName($Target.ExecutablePath)) `
            -WindowStyle Hidden `
            -PassThru
    } catch {
        return "failed"
    }
    if (-not $requestProcess.WaitForExit($script:maintenanceExitTimeoutSeconds * 1000)) {
        try {
            Stop-Process -Id $requestProcess.Id -Force -ErrorAction Stop
            $requestProcess.WaitForExit()
        } catch {
            Write-StopError "$productName maintenance exit process could not be terminated."
            exit $script:exitProbeFailed
        }
        # Only the disposable maintenance child is stopped here. The resident
        # application is never force-terminated.
        return "failed"
    }
    if ($requestProcess.ExitCode -eq 0) {
        return "exited"
    }
    if ($requestProcess.ExitCode -eq $script:applicationExitRejectedExitCode) {
        return "rejected"
    }
    return "failed"
}

$snapshot = Get-TargetSnapshot
if ($snapshot.Targets.Count -eq 0) {
    if ($snapshot.UnknownCount -gt 0) {
        Exit-ForUnverifiedProcess
    }
    exit $exitOk
}
if ($snapshot.UnknownCount -gt 0) {
    Write-StopError (
        "$productName process identity was unavailable for " +
        "$($snapshot.UnknownCount) process(es); continuing with verifiable ones."
    )
}

$windowSignalTargets = @(
    $snapshot.Targets | Where-Object {
        Test-WindowCapability `
            -Target $_ `
            -PropertyName $script:windowExitCapabilityProperty
    }
)
if ($windowSignalTargets.Count -gt 1) {
    Write-StopError "$productName has multiple current-session desktop owners."
    exit $exitUnsafeToContinue
}
if ($windowSignalTargets.Count -eq 1) {
    $windowExitResult = Invoke-WindowFullExit -Target $windowSignalTargets[0]
    if ($windowExitResult -eq "rejected") {
        Show-LegacyShellWindow -Targets $windowSignalTargets
        Write-StopError "$productName exit was cancelled by the user."
        if ($windowSignalTargets[0].ExternalLocation) {
            exit $exitOtherLocationRunning
        }
        exit $exitUserActionRequired
    }
    if ($windowExitResult -ne "exited") {
        Write-StopError "$productName did not complete its normal exit; files were not touched."
        exit $exitUnsafeToContinue
    }
    $completedSnapshot = Get-TargetSnapshot
    if ($completedSnapshot.Targets.Count -eq 0) {
        if ($completedSnapshot.UnknownCount -gt 0) {
            Exit-ForUnverifiedProcess
        }
        exit $exitOk
    }
    Write-StopError "$productName restarted while shutdown was being confirmed."
    exit $exitUnsafeToContinue
}

$v3Targets = @(
    $snapshot.Targets | Where-Object {
        Test-WindowCapability `
            -Target $_ `
            -PropertyName $script:v3ExitCapabilityProperty
    }
)
if ($v3Targets.Count -gt 1) {
    Write-StopError "$productName has multiple current-session desktop owners."
    exit $exitUnsafeToContinue
}
if ($v3Targets.Count -eq 1) {
    if ($isElevated) {
        Show-LegacyShellWindow -Targets $v3Targets
        Write-StopError "$productName requires manual confirmation before upgrade."
        if ($v3Targets[0].ExternalLocation) {
            exit $exitOtherLocationRunning
        }
        exit $exitUserActionRequired
    }
    $legacyV3ExitResult = Invoke-LegacyV3FullExit -Target $v3Targets[0]
    if ($legacyV3ExitResult -eq "rejected") {
        Show-LegacyShellWindow -Targets $v3Targets
        Write-StopError "$productName exit was cancelled by the user."
        if ($v3Targets[0].ExternalLocation) {
            exit $exitOtherLocationRunning
        }
        exit $exitUserActionRequired
    }
    if ($legacyV3ExitResult -ne "exited") {
        Write-StopError "$productName did not complete its normal exit; files were not touched."
        exit $exitUnsafeToContinue
    }
    $completedSnapshot = Get-TargetSnapshot
    if ($completedSnapshot.Targets.Count -eq 0) {
        if ($completedSnapshot.UnknownCount -gt 0) {
            Exit-ForUnverifiedProcess
        }
        exit $exitOk
    }
    Write-StopError "$productName restarted while shutdown was being confirmed."
    exit $exitUnsafeToContinue
}

$remainingSnapshot = Get-TargetSnapshot
if ($remainingSnapshot.Targets.Count -eq 0) {
    if ($remainingSnapshot.UnknownCount -gt 0) {
        Exit-ForUnverifiedProcess
    }
    exit $exitOk
}

$unknownRoles = @(
    $remainingSnapshot.Targets | Where-Object {
        [string]::IsNullOrWhiteSpace($_.CommandLine)
    }
)
if ($unknownRoles.Count -gt 0) {
    # 读不到命令行就无法区分“托盘桥”和“设置窗口”。此时先看实例互斥体：
    # 没有实例在运行说明这些进程不是当前桌面实例，安装可以继续；否则
    # 才按受限处理（提权重试）。
    Exit-ForUnverifiedProcess
}

$legacyBridgeTargets = @(
    $remainingSnapshot.Targets | Where-Object {
        $_.CommandLine -match '(?i)(^|\s|\")--bridge(\s|$|\")'
    }
)

if ($legacyBridgeTargets.Count -gt 0) {
    Initialize-WindowMethods
    foreach ($legacyBridgeTarget in $legacyBridgeTargets) {
        $postDeadline = [DateTime]::UtcNow.AddSeconds(2)
        $posted = $false
        $matchingWindowFound = $false
        $postError = 0
        do {
            $window = [RemoteMicInstaller.LegacyShellMethods]::FindWindowForProcessAndTitle(
                [uint32]$legacyBridgeTarget.ProcessId,
                $legacyBridgeWindowTitle
            )
            if ($window -ne [IntPtr]::Zero) {
                $matchingWindowFound = $true
                $posted = [RemoteMicInstaller.LegacyShellMethods]::PostMessage(
                    $window,
                    [uint32]$wmCommand,
                    [UIntPtr][uint32]$legacyExitCommand,
                    [IntPtr]::Zero
                )
                if ($posted) {
                    break
                }
                $postError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $postDeadline)

        if (-not $posted) {
            if (
                $matchingWindowFound -and
                $postError -eq $errorAccessDenied -and
                -not $isElevated
            ) {
                exit $exitNeedsElevation
            }
            if (-not $matchingWindowFound) {
                Write-StopError "$productName legacy bridge control window was not found."
            } else {
                Write-StopError "$productName legacy bridge could not receive its exit command."
            }
            exit $exitUnsafeToContinue
        }
    }

    if (-not (Wait-ForTargetsToExit `
        -Targets $legacyBridgeTargets `
        -TimeoutSeconds $legacyBridgeExitTimeoutSeconds
    )) {
        Write-StopError "$productName legacy bridge did not finish normal cleanup."
        exit $exitUnsafeToContinue
    }
}

# Candidate releases before the full-exit contract kept the settings shell
# separate from the bridge. Once every legacy bridge process has completed
# normal cleanup, leave the shell alive so the user can save or discard any
# pending mapping edits and choose its existing "完全退出" command. The
# installer must never trade a convenient upgrade for silent setting loss.
$shellSnapshot = Get-TargetSnapshot
if ($shellSnapshot.UnknownCount -gt 0) {
    Exit-ForRestrictedProcess
}
$restartedLegacyBridgeTargets = @(
    $shellSnapshot.Targets | Where-Object {
        $_.CommandLine -match '(?i)(^|\s|\")--bridge(\s|$|\")'
    }
)
if ($restartedLegacyBridgeTargets.Count -gt 0) {
    Write-StopError "$productName legacy bridge restarted during shutdown confirmation."
    exit $exitUnsafeToContinue
}
if ($shellSnapshot.Targets.Count -gt 0) {
    Show-LegacyShellWindow -Targets $shellSnapshot.Targets
    Write-StopError "$productName legacy settings require user confirmation before upgrade."
    if (@($shellSnapshot.Targets | Where-Object { $_.ExternalLocation }).Count -gt 0) {
        exit $exitOtherLocationRunning
    }
    exit $exitUserActionRequired
}

exit $exitOk
