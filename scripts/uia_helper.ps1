# uia_helper.ps1 -- persistent UI Automation helper for ExplorerDict.
#
# Protocol: one JSON request per line on stdin, one JSON response per line on stdout.
#   {"id":1,"cmd":"ping"}
#   {"id":2,"cmd":"selection","x":100,"y":200,"expectedHwnd":55501,"expectedPid":9999}
#   {"id":3,"cmd":"check","expectedHwnd":55501,"expectedPid":9999}
#   {"id":4,"cmd":"quit"}
#
# SAFETY: a "selection" / "check" request MUST carry expectedHwnd + expectedPid (the
# foreground top-level window captured *before* the gesture). Before touching any
# candidate element the helper verifies that the live foreground is still that window,
# and every candidate must belong to that top-level window / process. Candidates that
# fail the check are skipped WITHOUT calling TextPattern. If the check cannot be made,
# nothing is read (fail-closed). This does not remove every OS-level race; it removes
# the window in which the helper would read a game/other window.
#
# NOTE: this file is intentionally pure ASCII. Windows PowerShell 5.1 reads
# BOM-less .ps1 files using the ANSI code page, which would corrupt non-ASCII
# literals. Chinese characters are produced from code points instead, and all
# output goes through ConvertTo-Json, which escapes non-ASCII as \uXXXX.

$ErrorActionPreference = 'Stop'

$script:Utf8 = New-Object System.Text.UTF8Encoding($false)
$script:Out = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $script:Utf8)
$script:Out.AutoFlush = $true
$script:In = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $script:Utf8)

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch {
    $script:Out.WriteLine('{"ok":false,"reason":"uia_load_failed","error":"' + $_.Exception.Message.Replace('"', "'") + '"}')
    $script:Out.Flush()
    exit 2
}

$script:AE = [System.Windows.Automation.AutomationElement]
$script:TW = [System.Windows.Automation.TreeWalker]
$script:TS = [System.Windows.Automation.TreeScope]
$script:CT = [System.Windows.Automation.ControlType]
$script:TP = [System.Windows.Automation.TextPattern]
$script:VP = [System.Windows.Automation.ValuePattern]
$script:EP = [System.Windows.Automation.Text.TextPatternRangeEndpoint]
$script:TU = [System.Windows.Automation.Text.TextUnit]
$script:Root = $script:AE::RootElement

# 'di zhi' (address) -- built from code points to keep this file ASCII-only.
$script:AddrWord = ([char]0x5730).ToString() + ([char]0x5740).ToString()
$script:SearchWord = ([char]0x641C).ToString() + ([char]0x7D22).ToString()

# Bounded candidate budget. Reads are never attempted on more than this many
# candidates, and the fallback search runs ONLY under the expected (already
# verified) top-level window -- the desktop root is never enumerated.
$script:MaxCandidates = 18
$script:MaxAncestors = 4
$script:MaxTopDocuments = 3
$script:MaxTopTextControls = 6
$script:ProcessImages = @{}

# Chromium / Gecko -- and CEF-based suites such as WPS ("Prome" shell) -- expose
# page content from a CHILD helper process, so the element pid legitimately
# differs from the top-level window pid. A different element pid is accepted
# only when the candidate is anchored to the verified top-level window (its own
# top-level element IS that window: checked above) AND both processes run the
# SAME application (same image name, and the same full image path whenever both
# paths are readable). Any other pid difference stays fail-closed.
#
# 2026-10-09 (batch M27): the previous version accepted this only for a fixed
# list of browser classes/executables, so WPS -- foreground window owned by
# wps.exe, document elements provided by a second wps.exe process -- failed every
# read with candidate_element_pid_mismatch and the floating panel never opened.
# Reported by the user as "selecting text in WPS does nothing at all";
# reproduced without the app (Ctrl+A + the recall hotkey, 2026-10-09 21:02) and
# confirmed in data/logs/app.log as candidate_element_pid_mismatch.

function Write-Resp($obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 8
    $script:Out.WriteLine($json)
    $script:Out.Flush()
}

function Safe-Str($sb) {
    if ($null -eq $sb) { return '' }
    return [string]$sb
}

function Get-TopLevelElement($el) {
    if ($null -eq $el) { return $null }
    $walker = $script:TW::ControlViewWalker
    $cur = $el
    for ($i = 0; $i -lt 64; $i++) {
        try { $p = $walker.GetParent($cur) } catch { break }
        if ($null -eq $p) { break }
        if ($p -eq $script:Root) { break }
        $cur = $p
    }
    return $cur
}

function Get-ContextText($range, $chars) {
    # Returns @{ text = <context>; prefix = <offset of the selection start in text> }.
    # prefix comes from how far the start endpoint ACTUALLY moved
    # (MoveEndpointByUnit returns the real count), corrected by the amount of
    # leading whitespace trimmed off the context. The caller uses it to detect the
    # Chromium/Edge PDF selection quirk that drops the last character, so it can
    # complete a word tail only (see app/capture_service.complete_word_tail).
    # Unknown offset -> -1: the caller then completes nothing (fail closed).
    $none = @{ text = ''; prefix = -1 }
    if ($null -eq $range -or $chars -le 0) { return $none }
    try {
        $clone = $range.Clone()
        $moved = 0
        try {
            $moved = [int]$clone.MoveEndpointByUnit($script:EP::Start, $script:TU::Character, -1 * $chars)
        } catch { $moved = 0 }
        $null = $clone.MoveEndpointByUnit($script:EP::End, $script:TU::Character, $chars)
        $raw = Safe-Str $clone.GetText(-1)
        if ($raw.Length -eq 0) { return $none }
        $lead = $raw.Length - $raw.TrimStart().Length
        return @{ text = $raw.Trim(); prefix = [Math]::Max(0, $moved - $lead) }
    } catch {
        return $none
    }
}

function Get-ValueOf($el) {
    if ($null -eq $el) { return '' }
    try {
        $vp = $null
        if ($el.TryGetCurrentPattern($script:VP::Pattern, [ref]$vp)) {
            return (Safe-Str $vp.Current.Value)
        }
    } catch { }
    return ''
}

# ---------------------------------------------------------------- foreground gate
# Minimal Win32 foreground query (no injection, no memory reads, no modules).
try {
    # Built ahead of time: avoid invoking the C# compiler on first use/resume.
    $nativeDll = Join-Path $PSScriptRoot 'ExplorerDict.Native.dll'
    if (Test-Path -LiteralPath $nativeDll) {
        try { Add-Type -Path $nativeDll } catch { }
    }
    if (-not ([System.Management.Automation.PSTypeName]'ExplorerDict.Native').Type) {
        Add-Type -Namespace ExplorerDict -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetForegroundWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetAncestor(System.IntPtr hwnd, uint flags);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern uint GetWindowThreadProcessId(System.IntPtr hwnd, out uint processId);
[System.Runtime.InteropServices.DllImport("oleacc.dll")]
public static extern int AccessibleObjectFromWindow(System.IntPtr hwnd, uint objectId,
    ref System.Guid iid, [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Interface)] out object value);
'@
    }
} catch {
    # Without the helper type the foreground check cannot be made -> everything fails closed.
}

# Live foreground top-level HWND; 0 when it cannot be determined (fail-closed).
function Get-ForegroundHwnd {
    try {
        $t = [System.Management.Automation.PSTypeName]'ExplorerDict.Native'
        if (-not $t.Type) { return 0 }
        $h = [ExplorerDict.Native]::GetForegroundWindow()
        if ($null -eq $h) { return 0 }
        $v = $h.ToInt64()
        if ($v -le 0) { return 0 }
        return [int]$v
    } catch {
        return 0
    }
}

# Read expectedHwnd / expectedPid from a request. Returns $null when missing.
# NOTE: $pid is a read-only automatic variable in PowerShell -- never reuse that name.
function Get-ExpectedWindow($req) {
    $expHwnd = 0
    $expPid = 0
    try { if ($null -ne $req.expectedHwnd) { $expHwnd = [int]$req.expectedHwnd } } catch { $expHwnd = 0 }
    try { if ($null -ne $req.expectedPid) { $expPid = [int]$req.expectedPid } } catch { $expPid = 0 }
    if ($expHwnd -le 0 -or $expPid -le 0) { return $null }
    return @{ hwnd = $expHwnd; pid = $expPid }
}

# Is the live foreground still the expected window? (fail-closed)
function Test-ForegroundIs($exp) {
    if ($null -eq $exp) { return $false }
    $live = Get-ForegroundHwnd
    if ($live -le 0) { return $false }
    if ($live -ne $exp.hwnd) { return $false }
    return $true
}

# Does a candidate element belong to the expected top-level window / process?
# Returns '' when OK, otherwise a reason string. Runs BEFORE any Read-* call.
function Test-Candidate($el, $exp) {
    if ($null -eq $el) { return 'candidate_missing' }
    if ($null -eq $exp) { return 'expected_window_required' }
    $elPid = 0
    try { $elPid = [int]$el.Current.ProcessId } catch { $elPid = 0 }
    $top = $null
    try { $top = Get-TopLevelElement $el } catch { $top = $null }
    if ($null -eq $top) { return 'candidate_no_toplevel' }
    $topHwnd = 0
    try { $topHwnd = [int]$top.Current.NativeWindowHandle } catch { $topHwnd = 0 }
    if ($topHwnd -le 0) { return 'candidate_no_hwnd' }
    if ($topHwnd -ne $exp.hwnd) { return 'candidate_hwnd_mismatch' }
    $topPid = 0
    try { $topPid = [int]$top.Current.ProcessId } catch { $topPid = 0 }
    if ($topPid -le 0) { return 'candidate_no_pid' }
    if ($topPid -ne $exp.pid) { return 'candidate_pid_mismatch' }
    if ($elPid -gt 0 -and $elPid -ne $exp.pid) {
        # Same-application child process (browser renderer, WPS document host):
        # allowed only when both processes run the same executable.
        if (-not (Test-SameAppChildCandidate $el $exp)) {
            return 'candidate_element_pid_mismatch'
        }
    }
    return ''
}

# Image name (lower case, with extension) of a process; '' when unavailable.
function Get-ProcessIdentity($processId) {
    $key = [int]$processId
    $none = @{ name = ''; path = '' }
    if ($key -le 0) { return $none }
    if ($script:ProcessImages.ContainsKey($key)) { return $script:ProcessImages[$key] }
    try {
        $p = Get-Process -Id $key -ErrorAction Stop
        $name = ([string]$p.ProcessName).ToLowerInvariant() + '.exe'
        $path = ''
        try { $path = ([string]$p.Path).ToLowerInvariant() } catch { }
        $script:ProcessImages[$key] = @{ name = $name; path = $path }
    } catch { $script:ProcessImages[$key] = $none }
    return $script:ProcessImages[$key]
}

function Get-ProcessImageName($processId) {
    return (Get-ProcessIdentity $processId).name
}

# Full image path (lower case) of a process; '' when unavailable (access denied,
# 32/64-bit mismatch, process gone). Never throws.
function Get-ProcessImagePath($processId) {
    return (Get-ProcessIdentity $processId).path
}

# Is this element hosted by a CHILD process of the SAME application as the
# verified top-level window? (browser renderer, WPS document host, Electron
# helper, Office add-in host...). The anchoring to the expected top-level window
# was already verified by Test-Candidate, so only "same application" is checked:
# same image name, plus the same full image path when both paths are readable.
function Test-SameAppChildCandidate($el, $exp) {
    if ($null -eq $el -or $null -eq $exp) { return $false }
    $elPid = 0
    try { $elPid = [int]$el.Current.ProcessId } catch { return $false }
    if ($elPid -le 0) { return $false }
    $topName = Get-ProcessImageName $exp.pid
    $elName = Get-ProcessImageName $elPid
    if ([string]::IsNullOrEmpty($topName) -or [string]::IsNullOrEmpty($elName)) { return $false }
    if ($topName -ne $elName) { return $false }
    $topPath = Get-ProcessImagePath $exp.pid
    $elPath = Get-ProcessImagePath $elPid
    if ((-not [string]::IsNullOrEmpty($topPath)) -and (-not [string]::IsNullOrEmpty($elPath))) {
        if ($topPath -ne $elPath) { return $false }
    }
    return $true
}

# Append a candidate while the bounded budget allows it.
function Add-Candidate($list, $el, $src) {
    if ($null -eq $el) { return }
    if ($list.Count -ge $script:MaxCandidates) { return }
    $null = $list.Add(@{ el = $el; src = $src })
}

# Bounded ancestor walk (the focused / clicked node itself often has no
# TextPattern; the document a few levels up does).
function Add-AncestorCandidates($list, $el, $srcPrefix) {
    if ($null -eq $el) { return }
    $walker = $script:TW::ControlViewWalker
    $cur = $el
    for ($i = 0; $i -lt $script:MaxAncestors; $i++) {
        if ($list.Count -ge $script:MaxCandidates) { return }
        try { $p = $walker.GetParent($cur) } catch { return }
        if ($null -eq $p) { return }
        if ($p -eq $script:Root) { return }
        Add-Candidate $list $p ($srcPrefix + '_ancestor' + ($i + 1))
        $cur = $p
    }
}

# Bounded fallback search INSIDE the verified top-level window (Document /
# Edit controls). Never searches the desktop; every candidate still has to pass
# Test-Candidate before TextPattern is touched.
function Get-BoundedElements($top, $exp, $skipDocuments = $false) {
    $result = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue(@{ el = $top; depth = 0 })
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $visited = 0
    $walker = $script:TW::ControlViewWalker
    while ($queue.Count -gt 0 -and $visited -lt 160 -and $timer.ElapsedMilliseconds -lt 450) {
        if (-not (Test-ForegroundIs $exp)) { break }
        $node = $queue.Dequeue()
        $visited++
        $el = $node.el
        $null = $result.Add($el)
        if ($node.depth -ge 12) { continue }
        try {
            if ($skipDocuments -and $el.Current.ControlType -eq $script:CT::Document) { continue }
            $child = $walker.GetFirstChild($el)
            while ($null -ne $child -and ($visited + $queue.Count) -lt 160 -and $timer.ElapsedMilliseconds -lt 450) {
                if (-not (Test-ForegroundIs $exp)) { break }
                $queue.Enqueue(@{ el = $child; depth = ($node.depth + 1) })
                $child = $walker.GetNextSibling($child)
            }
        } catch { }
    }
    return $result.ToArray()
}

function Add-TopLevelCandidates($list, $exp) {
    if ($null -eq $exp -or -not (Test-ForegroundIs $exp)) { return }
    try { $top = $script:AE::FromHandle([IntPtr]$exp.hwnd) } catch { return }
    if ($null -eq $top -or (Test-Candidate $top $exp) -ne '') { return }
    $docCount = 0
    $textCount = 0
    foreach ($el in (Get-BoundedElements $top $exp)) {
        if ($list.Count -ge $script:MaxCandidates -or -not (Test-ForegroundIs $exp)) { break }
        try {
            if ($el.Current.ControlType -eq $script:CT::Document -and $docCount -lt $script:MaxTopDocuments) {
                Add-Candidate $list $el 'document'
                $docCount++
            } elseif ($textCount -lt $script:MaxTopTextControls -and
                      [bool]$el.GetCurrentPropertyValue($script:AE::IsTextPatternAvailableProperty)) {
                Add-Candidate $list $el 'top_text'
                $textCount++
            }
        } catch { }
    }
}

# Address metadata is optional: never enumerate all document descendants for it.
function Get-WindowUrl($win, $exp) {
    if ($null -eq $win -or -not (Test-ForegroundIs $exp)) { return @{ url = ''; how = '' } }
    # Native Office documents have no browser address bar. Avoid traversing
    # their large ribbons after a successful selection just to obtain no URL.
    if ((Get-ProcessImageName $exp.pid) -in @('wps.exe', 'winword.exe')) {
        return @{ url = ''; how = '' }
    }
    $ids = @('addressEditBox', 'omnibox', 'urlbar-input', 'address-bar', 'AddressEditBox', 'urlBar')
    foreach ($el in (Get-BoundedElements $win $exp $true)) {
        if (-not (Test-ForegroundIs $exp)) { break }
        try {
            $id = [string]$el.Current.AutomationId
            $nm = [string]$el.Current.Name
            $knownId = $ids -contains $id
            $addressEdit = ($el.Current.ControlType -eq $script:CT::Edit) -and
                ($nm -match '(?i)address' -or $nm.Contains($script:AddrWord))
            if (($knownId -or $addressEdit) -and (Test-Candidate $el $exp) -eq '') {
                $v = Get-ValueOf $el
                if ($v -and ($knownId -or $v -match '^[a-zA-Z][a-zA-Z0-9+.\-]*://')) {
                    return @{ url = $v.Trim(); how = 'bounded_address' }
                }
            }
        } catch { }
    }
    return @{ url = ''; how = '' }
}

function Get-ElementIdentity($el) {
    $info = @{
        element_name = ''
        element_class = ''
        control_type = ''
        process_id = 0
        hwnd = 0
        top_title = ''
    }
    if ($null -eq $el) { return $info }
    try { $info.element_name = Safe-Str $el.Current.Name } catch { }
    try { $info.element_class = Safe-Str $el.Current.ClassName } catch { }
    try { $info.control_type = Safe-Str $el.Current.ControlType.ProgrammaticName } catch { }
    try { $info.process_id = [int]$el.Current.ProcessId } catch { }
    try { $info.hwnd = [int]$el.Current.NativeWindowHandle } catch { }
    try {
        $top = Get-TopLevelElement $el
        if ($null -ne $top) { $info.top_title = Safe-Str $top.Current.Name }
    } catch { }
    return $info
}

function Try-SelectionFrom($el, $chars) {
    $result = @{ ok = $false; reason = 'no_element'; text = ''; context = ''
                 context_prefix = -1; method = ''; full = '' }
    if ($null -eq $el) { return $result }
    $tp = $null
    try {
        if (-not $el.TryGetCurrentPattern($script:TP::Pattern, [ref]$tp)) {
            $result.reason = 'no_textpattern'
            return $result
        }
    } catch {
        $result.reason = 'textpattern_error'
        return $result
    }
    try {
        $ranges = $tp.GetSelection()
    } catch {
        $result.reason = 'getselection_error'
        return $result
    }
    if ($null -eq $ranges -or $ranges.Length -eq 0) {
        $result.reason = 'empty_selection'
        return $result
    }
    $parts = New-Object System.Collections.ArrayList
    foreach ($r in $ranges) {
        try { $null = $parts.Add((Safe-Str $r.GetText(-1))) } catch { }
    }
    $text = ($parts -join '')
    if ($text.Trim().Length -eq 0) {
        $result.reason = 'empty_text'
        return $result
    }
    $result.ok = $true
    $result.reason = ''
    $result.text = $text
    $result.method = 'TextPattern'
    # context_prefix is only meaningful for the LAST range; with several ranges
    # text is a concatenation of them, so the caller's startswith() check fails
    # and it completes nothing (fail closed).
    $ctx = Get-ContextText $ranges[$ranges.Length - 1] $chars
    $result.context = $ctx.text
    $result.context_prefix = $ctx.prefix
    return $result
}

function Get-DefaultContextChars($v) {
    if ($null -eq $v) { return 120 }
    $n = 0
    if (-not [int]::TryParse([string]$v, [ref]$n)) { return 120 }
    if ($n -lt 0) { return 0 }
    if ($n -gt 1000) { return 1000 }
    return $n
}

# WPS exposes its document view without TextPattern on some versions. Bind the
# native Office object model to THAT verified body HWND, never to a global
# running application or a newly created COM instance. This also supports Word
# body views. No clipboard access, keystrokes, document edits or app startup.
function Test-OfficeHwnd($handle, $exp) {
    if ($null -eq $exp -or $handle -le 0 -or -not (Test-ForegroundIs $exp)) { return $false }
    try {
        $root = [ExplorerDict.Native]::GetAncestor([IntPtr]$handle, 2).ToInt64()
        if ($root -ne $exp.hwnd) { return $false }
        $topProcess = [uint32]0
        $null = [ExplorerDict.Native]::GetWindowThreadProcessId([IntPtr]$root, [ref]$topProcess)
        if ($topProcess -ne $exp.pid) { return $false }
        $bodyProcess = [uint32]0
        $null = [ExplorerDict.Native]::GetWindowThreadProcessId([IntPtr]$handle, [ref]$bodyProcess)
        if ($bodyProcess -le 0) { return $false }
        $topName = Get-ProcessImageName $exp.pid
        if ($topName -notin @('wps.exe', 'winword.exe')) { return $false }
        if ((Get-ProcessImageName $bodyProcess) -ne $topName) { return $false }
        $topPath = Get-ProcessImagePath $exp.pid
        $bodyPath = Get-ProcessImagePath $bodyProcess
        if ($topPath -and $bodyPath -and $topPath -ne $bodyPath) { return $false }
        return $true
    } catch { return $false }
}

function Get-NativeOfficeWindow($handle) {
    $iid = [Guid]'00020400-0000-0000-C000-000000000046'
    $window = $null
    $hr = [ExplorerDict.Native]::AccessibleObjectFromWindow(
        [IntPtr]$handle, [uint32]4294967280, [ref]$iid, [ref]$window)
    if ($hr -ne 0) { return $null }
    return $window
}

function Try-OfficeSelectionFrom($el, $exp, $chars) {
    $result = @{ ok = $false; reason = 'office_selection_unavailable'; text = ''; context = ''
                 context_prefix = -1; method = ''; full = '' }
    $window = $null
    $selection = $null
    $range = $null
    $clone = $null
    try {
        # Restrict fallback to actual document body views. Ribbon/search fields
        # must never return an old selection left in the document.
        if ((Test-Candidate $el $exp) -ne '' -or -not (Test-ForegroundIs $exp)) { return $result }
        if ($el.Current.ClassName -notin @('_WwG', 'KxWpsView')) { return $result }
        $handle = [int]$el.Current.NativeWindowHandle
        if (-not (Test-OfficeHwnd $handle $exp)) { return $result }
        $window = Get-NativeOfficeWindow $handle
        if ($null -eq $window) { return $result }
        $officeHandle = [int]$window.Hwnd
        if (-not (Test-OfficeHwnd $officeHandle $exp)) { return $result }
        $selection = $window.Selection
        # Normal contiguous text only: insertion points and block/shape
        # selections are not reliable text ranges.
        if ([int]$selection.Type -ne 2) {
            $result.reason = 'empty_selection'
            return $result
        }
        $range = $selection.Range
        $start = [int]$range.Start
        $end = [int]$range.End
        if ($end -le $start) { $result.reason = 'empty_selection'; return $result }
        if (-not (Test-OfficeHwnd $officeHandle $exp)) { return $result }
        $raw = [string]$range.Text
        $text = $raw.Trim()
        if (-not $text) { $result.reason = 'empty_text'; return $result }
        if ($chars -gt 0) {
            try {
                # Duplicate range operations move clone endpoints only, not the
                # user's selection, caret, document text or clipboard.
                $clone = $range.Duplicate
                $null = $clone.MoveStart(1, -1 * $chars)
                $null = $clone.MoveEnd(1, $chars)
                if (-not (Test-OfficeHwnd $officeHandle $exp)) { return $result }
                $ctxRaw = [string]$clone.Text
                $lead = $ctxRaw.Length - $ctxRaw.TrimStart().Length
                $selLead = $raw.Length - $raw.TrimStart().Length
                $result.context = $ctxRaw.Trim()
                $result.context_prefix = [Math]::Max(0, $start + $selLead - [int]$clone.Start - $lead)
            } catch { $result.context = ''; $result.context_prefix = -1 }
        }
        # Reject a changed native active window as well as a changed foreground.
        if ([int]$window.Hwnd -ne $officeHandle -or -not (Test-OfficeHwnd $officeHandle $exp)) { return $result }
        $result.ok = $true
        $result.reason = ''
        $result.text = $text
        $result.method = 'OfficeSelection'
        return $result
    } catch { return $result }
    finally {
        foreach ($obj in @($clone, $range, $selection, $window)) {
            if ($null -ne $obj -and [System.Runtime.InteropServices.Marshal]::IsComObject($obj)) {
                try { $null = [System.Runtime.InteropServices.Marshal]::ReleaseComObject($obj) } catch { }
            }
        }
    }
}

function Handle-Selection($req) {
    $exp = Get-ExpectedWindow $req
    if ($null -eq $exp) {
        # No expectation to verify against -> fail closed, read nothing.
        return @{ ok = $false; reason = 'expected_window_required'; text = ''; context = ''
                  method = ''; reads = 0 }
    }
    # Re-check the live foreground BEFORE building/reading any candidate.
    if (-not (Test-ForegroundIs $exp)) {
        return @{ ok = $false; reason = 'foreground_changed'; text = ''; context = ''
                  method = ''; reads = 0 }
    }

    $chars = Get-DefaultContextChars $req.contextChars
    $candidates = New-Object System.Collections.ArrayList
    $focused = $null
    $at = $null
    try { $focused = $script:AE::FocusedElement } catch { }
    if ($null -ne $focused) {
        Add-Candidate $candidates $focused 'focused'
    }

    if ($null -ne $req.x -and $null -ne $req.y) {
        try {
            $pt = New-Object System.Windows.Point([double]$req.x, [double]$req.y)
            $at = $script:AE::FromPoint($pt)
            if ($null -ne $at) {
                Add-Candidate $candidates $at 'frompoint'
            }
        } catch { }
    }

    # reads = verified candidate selection attempts (including pattern probes
    # and native Office fallback). It stays 0 when identity checks fail.
    $reads = 0
    $lastReason = 'no_candidate_element'
    $index = 0
    $topSearched = $false
    $ancestorsAdded = $false
    $result = $null
    if ($candidates.Count -eq 0) {
        $topSearched = $true
        Add-TopLevelCandidates $candidates $exp
    }
    while ($index -lt $candidates.Count) {
        if ($index -ge $script:MaxCandidates) { break }
        $cand = $candidates[$index]
        $index = $index + 1
        # The foreground may have changed mid-request: re-check before EVERY read.
        # (Not a claim that all OS races are gone -- just that we never read a window
        # that is not the expected one at check time.)
        if (-not (Test-ForegroundIs $exp)) {
            $lastReason = 'foreground_changed'
            break
        }
        # Candidate must belong to the expected top-level window / process.
        $bad = Test-Candidate $cand.el $exp
        if ($bad -ne '') {
            $lastReason = $bad
        } else {
            $reads = $reads + 1
            $r = Try-SelectionFrom $cand.el $chars
            if (-not $r.ok -and $r.reason -eq 'no_textpattern') {
                $office = Try-OfficeSelectionFrom $cand.el $exp $chars
                if ($office.ok -or $office.reason -in @('empty_selection', 'empty_text')) { $r = $office }
            }
            if ($r.ok) {
                $id = Get-ElementIdentity $cand.el
                $win = Get-TopLevelElement $cand.el
                if (-not (Test-ForegroundIs $exp)) {
                    $lastReason = 'foreground_changed'
                    break
                }
                $urlInfo = Get-WindowUrl $win $exp
                # process_id / hwnd are the VERIFIED top-level identity (the one
                # the caller checks against the foreground). A browser child
                # renderer pid is reported separately and never as process_id.
                $result = @{
                    ok           = $true
                    reason       = ''
                    text         = $r.text
                    context      = $r.context
                    context_prefix = $r.context_prefix
                    method       = $r.method
                    found_via    = $cand.src
                    url          = $urlInfo.url
                    url_how      = $urlInfo.how
                    top_title    = $id.top_title
                    element_name = $id.element_name
                    element_class = $id.element_class
                    control_type = $id.control_type
                    process_id   = $exp.pid
                    hwnd         = $exp.hwnd
                    element_process_id = $id.process_id
                    element_hwnd = $id.hwnd
                    reads        = $reads
                }
                break
            }
            $lastReason = $r.reason
        }
        # Try focused/point first; expand ancestors only when they failed.
        # This avoids UIA tree walks on a normal successful body selection.
        if (-not $ancestorsAdded -and $index -ge $candidates.Count) {
            $ancestorsAdded = $true
            Add-AncestorCandidates $candidates $focused 'focused'
            Add-AncestorCandidates $candidates $at 'frompoint'
        }
        # All direct candidates (focused / point + bounded ancestors) failed:
        # do ONE bounded search inside the verified top-level window.
        if (-not $topSearched -and $index -ge $candidates.Count) {
            $topSearched = $true
            Add-TopLevelCandidates $candidates $exp
        }
    }
    if ($null -ne $result) { return $result }
    return @{ ok = $false; reason = $lastReason; text = ''; context = ''; method = ''
              reads = $reads; candidates = $candidates.Count; top_search = $topSearched }
}

function Handle-Check($req) {
    $exp = Get-ExpectedWindow $req
    if ($null -eq $exp) {
        return @{ ok = $false; reason = 'expected_window_required' }
    }
    if (-not (Test-ForegroundIs $exp)) {
        return @{ ok = $false; reason = 'foreground_changed' }
    }
    $out = @{ ok = $true; focused = $null; textpattern_available = $false; top_title = ''; url = '' }
    try {
        $focused = $script:AE::FocusedElement
        if ($null -ne $focused) {
            $bad = Test-Candidate $focused $exp
            if ($bad -ne '') {
                return @{ ok = $false; reason = $bad }
            }
            $info = Get-ElementIdentity $focused
            $out.focused = $info
            $tp = $null
            try { $out.textpattern_available = [bool]$focused.TryGetCurrentPattern($script:TP::Pattern, [ref]$tp) } catch { }
            $win = Get-TopLevelElement $focused
            $urlInfo = Get-WindowUrl $win $exp
            $out.url = $urlInfo.url
        }
    } catch {
        return @{ ok = $false; reason = 'check_failed'; error = $_.Exception.Message }
    }
    return $out
}

# ------------------------------------------------------------------ main loop
while ($true) {
    $line = $null
    try { $line = $script:In.ReadLine() } catch { break }
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line.Length -eq 0) { continue }

    $req = $null
    try {
        $req = $line | ConvertFrom-Json
    } catch {
        Write-Resp @{ ok = $false; reason = 'bad_request'; error = 'invalid json' }
        continue
    }

    $id = 0
    try { if ($null -ne $req.id) { $id = [int]$req.id } } catch { $id = 0 }
    $cmd = ''
    try { $cmd = [string]$req.cmd } catch { $cmd = '' }

    if ($cmd -eq 'quit') {
        Write-Resp @{ id = $id; ok = $true; bye = $true }
        break
    }

    $resp = $null
    # Executable identities are immutable during a process lifetime. Reuse them
    # only within this request; never retain a cached PID across requests.
    $script:ProcessImages = @{}
    try {
        switch ($cmd) {
            'ping' {
                $resp = @{ ok = $true; pong = $true; version = 1; ps = $PSVersionTable.PSVersion.ToString() }
            }
            'selection' {
                $resp = Handle-Selection $req
            }
            'check' {
                $resp = Handle-Check $req
            }
            default {
                $resp = @{ ok = $false; reason = 'unknown_command'; cmd = $cmd }
            }
        }
    } catch {
        $resp = @{ ok = $false; reason = 'exception'; error = $_.Exception.Message }
    }

    $resp['id'] = $id
    Write-Resp $resp
}

try { $script:Out.Flush() } catch { }
exit 0
