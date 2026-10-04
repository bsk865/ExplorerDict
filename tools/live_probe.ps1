# live_probe.ps1 -- acceptance-test helper (ASCII only, see uia_helper.ps1 for why).
#
# Usage:
#   powershell -File tools/live_probe.ps1 -Action rect   -Marker "marked word"
#   powershell -File tools/live_probe.ps1 -Action select -Marker "marked word"
#
# Prints one JSON line. Used only by tools/live_capture_check.py.

param(
    [string]$Action = 'rect',
    [string]$Marker = ''
)

$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Out = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $Utf8)
$Out.AutoFlush = $true

function Emit($obj) { $Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 8)); $Out.Flush() }

Add-Type -Namespace '' -Name 'NativeFg' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetForegroundWindow();
'@

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch {
    Emit @{ ok = $false; reason = 'uia_load_failed'; error = $_.Exception.Message }
    exit 2
}

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$TP = [System.Windows.Automation.TextPattern]

$hwnd = [NativeFg]::GetForegroundWindow()
if ($hwnd -eq [IntPtr]::Zero) {
    Emit @{ ok = $false; reason = 'no_foreground_window' }
    exit 0
}
$win = $AE::FromHandle($hwnd)
if ($null -eq $win) {
    Emit @{ ok = $false; reason = 'from_handle_failed' }
    exit 0
}

$title = ''
try { $title = [string]$win.Current.Name } catch { }

# Collect candidate elements that expose TextPattern (Document, Edit, then the window).
$candidates = New-Object System.Collections.ArrayList
foreach ($ct in @($CT::Document, $CT::Edit)) {
    try {
        $cond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $ct)
        foreach ($d in $win.FindAll($TS::Descendants, $cond)) { $null = $candidates.Add($d) }
    } catch { }
}
$null = $candidates.Add($win)

$target = $null
$tp = $null
$docRange = $null
foreach ($c in $candidates) {
    try {
        $p = $null
        if ($c.TryGetCurrentPattern($TP::Pattern, [ref]$p)) {
            $r = $p.DocumentRange
            if ($null -ne $r) { $target = $c; $tp = $p; $docRange = $r; break }
        }
    } catch { }
}

if ($null -eq $target) {
    Emit @{ ok = $false; reason = 'no_textpattern'; window_title = $title }
    exit 0
}

$found = $null
if ($Marker -ne '') {
    try { $found = $docRange.FindText($Marker, $false, $false) } catch { $found = $null }
}
if ($null -eq $found) {
    Emit @{ ok = $false; reason = 'marker_not_found'; window_title = $title; marker = $Marker }
    exit 0
}

$rects = New-Object System.Collections.ArrayList
try {
    $raw = $found.GetBoundingRectangles()
    for ($i = 0; $i + 3 -lt $raw.Length; $i += 4) {
        $null = $rects.Add(@([double]$raw[$i], [double]$raw[$i + 1],
                             [double]$raw[$i + 2], [double]$raw[$i + 3]))
    }
} catch { }

$selectedText = ''
try { $selectedText = [string]$found.GetText(-1) } catch { }

if ($Action -eq 'select') {
    try {
        $found.Select()
    } catch {
        Emit @{ ok = $false; reason = 'select_failed'; error = $_.Exception.Message
                window_title = $title }
        exit 0
    }
}

Emit @{
    ok            = $true
    window_title  = $title
    rects         = @($rects)
    rect_count    = $rects.Count
    action        = $Action
    marker        = $Marker
    selected_text = $selectedText
}
exit 0
