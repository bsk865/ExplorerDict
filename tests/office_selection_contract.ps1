param([string]$HelperPath)
$ErrorActionPreference = 'Stop'
# Execute the actual helper functions with controlled native/COM providers.
# Parse definitions only: no UIA root, real windows or COM applications touched.
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($HelperPath,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Helper parse failed' }
$functions=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$false)
foreach ($name in @('Test-OfficeHwnd','Try-OfficeSelectionFrom','Get-ProcessIdentity')) {
    $code=($functions | Where-Object Name -eq $name).Extent.Text
    Invoke-Expression ($code.Replace('[ExplorerDict.Native]','[OfficeContract.Native]'))
}
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
namespace OfficeContract {
 public static class Native {
  public static bool Foreground = true;
  public static Dictionary<long,long> Roots = new Dictionary<long,long>();
  public static Dictionary<long,uint> Processes = new Dictionary<long,uint>();
  public static IntPtr GetAncestor(IntPtr hwnd, uint flags) { return new IntPtr(Roots[hwnd.ToInt64()]); }
  public static uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId) { processId=Processes[hwnd.ToInt64()]; return 1; }
 }
 public class Range {
  public string Body; public int Start; public int End; public bool SwitchOnRead;
  public string Text { get { if (SwitchOnRead) Native.Foreground=false; return Body.Substring(Start,End-Start); } }
  public Range Duplicate { get { return new Range { Body=Body,Start=Start,End=End }; } }
  public int MoveStart(int unit,int count) { int old=Start; Start=Math.Max(0,Start+count); return Start-old; }
  public int MoveEnd(int unit,int count) { int old=End; End=Math.Min(Body.Length,End+count); return End-old; }
 }
 public class Selection {
  public int Type=2; public int Reads; public Range Value;
  public Range Range { get { Reads++; return Value; } }
 }
 public class Window { public int Hwnd=20; public Selection Selection; }
}
'@
function Test-ForegroundIs($exp) { return [OfficeContract.Native]::Foreground }
function Test-Candidate($el,$exp) { return $script:CandidateError }
function Get-ProcessImageName($processId) { return $script:Names[[int]$processId] }
function Get-ProcessImagePath($processId) { return $script:Paths[[int]$processId] }
function Get-NativeOfficeWindow($handle) { $script:Binds++; return $script:Window }
function Reset-Fixture {
    [OfficeContract.Native]::Foreground=$true
    [OfficeContract.Native]::Roots.Clear()
    [OfficeContract.Native]::Processes.Clear()
    foreach ($h in @(10,20,30)) { [OfficeContract.Native]::Roots[$h]=10 }
    [OfficeContract.Native]::Processes[10]=100
    [OfficeContract.Native]::Processes[20]=200
    [OfficeContract.Native]::Processes[30]=200
    $script:Names=@{100='wps.exe';200='wps.exe'}
    $script:Paths=@{100='c:/wps.exe';200='c:/wps.exe'}
    $script:CandidateError=''
    $script:Binds=0
    $script:Window=New-Object OfficeContract.Window
    $script:Window.Selection=New-Object OfficeContract.Selection
    $script:Window.Selection.Value=New-Object OfficeContract.Range
    $script:Window.Selection.Value.Body='alpha beta gamma'
    $script:Window.Selection.Value.Start=6
    $script:Window.Selection.Value.End=10
    $script:El=[pscustomobject]@{Current=[pscustomobject]@{ClassName='KxWpsView';NativeWindowHandle=30}}
}
$exp=@{hwnd=10;pid=100}
$script:Count=0
function Assert($condition,$message) { if (-not $condition) { throw $message }; $script:Count++ }
Reset-Fixture
$r=Try-OfficeSelectionFrom $script:El $exp 3
Assert ($r.ok -and $r.text -eq 'beta' -and $r.method -eq 'OfficeSelection') 'Normal selection must be read'
Assert ($r.context -eq 'ha beta ga' -and $r.context_prefix -eq 3) 'Context and selection offset must agree'
Assert ($script:Window.Selection.Value.Start -eq 6 -and $script:Window.Selection.Value.End -eq 10) 'Clone must not move the actual selection'
Reset-Fixture
$script:El.Current.ClassName='_WwG'; $script:Names[100]='winword.exe'; $script:Names[200]='winword.exe'
Assert ((Try-OfficeSelectionFrom $script:El $exp 0).ok) 'Word body and zero context supported'
foreach ($case in @('foreground','candidate','ribbon','root','top_pid','child_image','child_path','unknown_app')) {
    Reset-Fixture
    switch ($case) {
        'foreground' { [OfficeContract.Native]::Foreground=$false }
        'candidate' { $script:CandidateError='candidate_hwnd_mismatch' }
        'ribbon' { $script:El.Current.ClassName='KSearchEdit' }
        'root' { [OfficeContract.Native]::Roots[30]=999 }
        'top_pid' { [OfficeContract.Native]::Processes[10]=999 }
        'child_image' { $script:Names[200]='other.exe' }
        'child_path' { $script:Paths[200]='c:/other/wps.exe' }
        'unknown_app' { $script:Names[100]='other.exe'; $script:Names[200]='other.exe' }
    }
    $r=Try-OfficeSelectionFrom $script:El $exp 3
    Assert (-not $r.ok -and $script:Binds -eq 0 -and $script:Window.Selection.Reads -eq 0) ($case + ' must prevent native binding/text read')
}
Reset-Fixture
$script:Window.Hwnd=999
Assert (-not (Try-OfficeSelectionFrom $script:El $exp 3).ok -and $script:Window.Selection.Reads -eq 0) 'Returned native window must be verified before range read'
foreach ($kind in @(1,6)) {
    Reset-Fixture; $script:Window.Selection.Type=$kind
    $r=Try-OfficeSelectionFrom $script:El $exp 3
    Assert (-not $r.ok -and $r.reason -eq 'empty_selection' -and $script:Window.Selection.Reads -eq 0) 'Caret/block selections must not become words'
}
Reset-Fixture; $script:Window.Selection.Value.End=6
Assert (-not (Try-OfficeSelectionFrom $script:El $exp 3).ok) 'Collapsed range must not be read as a word'
Reset-Fixture; $script:Window.Selection.Value.SwitchOnRead=$true
Assert (-not (Try-OfficeSelectionFrom $script:El $exp 3).ok) 'Foreground change during range read must discard result'
$script:ProcessImages=@{}; $script:ProcessQueries=0; $script:FakePath='c:/wps.exe'
function Get-Process { param($Id,$ErrorAction) $script:ProcessQueries++; return [pscustomobject]@{ProcessName='wps';Path=$script:FakePath} }
$first=Get-ProcessIdentity 100
$second=Get-ProcessIdentity ([uint32]100)
Assert ($first.name -eq 'wps.exe' -and $second.path -eq 'c:/wps.exe' -and $script:ProcessQueries -eq 1) 'Reuse process identity only within current request'
$script:ProcessImages=@{}; $script:FakePath='c:/changed/wps.exe'
Assert ((Get-ProcessIdentity 100).path -eq $script:FakePath -and $script:ProcessQueries -eq 2) 'New request must re-query process identity'
Write-Output ('PASS assertions=' + $script:Count)
