param(
    [Parameter(Mandatory = $true)][string]$InstallationArchive,
    [Parameter(Mandatory = $true)][string]$SetupExecutable
)

$ErrorActionPreference = "Stop"
$workingDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("LightSetupBuild-" + [guid]::NewGuid().ToString("N"))
try {
    $InstallationArchive = [System.IO.Path]::GetFullPath($InstallationArchive)
    $SetupExecutable = [System.IO.Path]::GetFullPath($SetupExecutable)
    if (Test-Path -LiteralPath $SetupExecutable) { throw "Output already exists; it will not be overwritten: $SetupExecutable" }
    if (-not (Test-Path -LiteralPath $InstallationArchive -PathType Leaf)) { throw "Installation ZIP not found." }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($InstallationArchive)
    try {
        $manifestEntry = $archive.GetEntry("PewVista/release.json")
        if (-not $manifestEntry) { $manifestEntry = $archive.GetEntry("The Light Ministries/release.json") }
        if (-not $manifestEntry) { throw "Installation manifest is missing." }
        $reader = New-Object System.IO.StreamReader($manifestEntry.Open())
        try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
        if ($manifest.application -ne "The Light Ministries" -or $manifest.package_type -ne "installation") { throw "Select an Installation ZIP, not an Update ZIP." }
    }
    finally { $archive.Dispose() }
    [System.IO.Directory]::CreateDirectory($workingDirectory) | Out-Null
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($SetupExecutable)) | Out-Null
    $bootstrapPath = Join-Path $PSScriptRoot "setup_windows_self_extract.ps1"
    $bootstrap = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($bootstrapPath))
    $source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class SetupLauncher
{
    private const string Bootstrap = "__BOOTSTRAP__";

    private static long FindPayloadEnd(FileStream source, BinaryReader reader)
    {
        // A code signature is appended after the setup payload; Windows records its
        // position in the PE security directory, so the payload trailer ends there.
        long end = source.Length;
        try
        {
            source.Position = 0x3C;
            int peOffset = reader.ReadInt32();
            source.Position = peOffset + 24;
            ushort magic = reader.ReadUInt16();
            long securityEntry = peOffset + 24 + (magic == 0x20b ? 112 : 96) + 4 * 8;
            source.Position = securityEntry;
            uint certificateOffset = reader.ReadUInt32();
            uint certificateSize = reader.ReadUInt32();
            if (certificateOffset > 0 && certificateSize > 0 && certificateOffset + (long)certificateSize >= source.Length - 8)
            {
                // Signing pads the file to an 8-byte boundary before the signature.
                for (long candidate = certificateOffset; candidate >= Math.Max(48, (long)certificateOffset - 7); candidate--)
                {
                    source.Position = candidate - 8;
                    if (Encoding.ASCII.GetString(reader.ReadBytes(8)) == "TLSETUP1") { end = candidate; break; }
                }
            }
        }
        catch (EndOfStreamException) { }
        return end;
    }

    private static void CopyPackage(Stream destination, IProgress<int> progress)
    {
        using (FileStream source = File.OpenRead(Application.ExecutablePath))
        using (BinaryReader reader = new BinaryReader(source))
        {
            long end = FindPayloadEnd(source, reader);
            if (end < 48) throw new InvalidDataException("The setup file is incomplete.");
            source.Position = end - 48;
            long length = reader.ReadInt64();
            byte[] expectedHash = reader.ReadBytes(32);
            if (Encoding.ASCII.GetString(reader.ReadBytes(8)) != "TLSETUP1" || length <= 0 || length > end - 48)
                throw new InvalidDataException("The setup package is invalid.");
            source.Position = end - 48 - length;
            using (SHA256 hash = SHA256.Create())
            {
                byte[] buffer = new byte[1024 * 1024];
                long remaining = length;
                int lastPercent = -1;
                while (remaining > 0)
                {
                    int count = source.Read(buffer, 0, (int)Math.Min(buffer.Length, remaining));
                    if (count == 0) throw new EndOfStreamException("The setup download is incomplete.");
                    hash.TransformBlock(buffer, 0, count, buffer, 0);
                    destination.Write(buffer, 0, count);
                    remaining -= count;
                    int percent = (int)((length - remaining) * 100L / length);
                    if (progress != null && percent != lastPercent) { progress.Report(percent); lastPercent = percent; }
                }
                hash.TransformFinalBlock(new byte[0], 0, 0);
                if (!Convert.ToBase64String(hash.Hash).Equals(Convert.ToBase64String(expectedHash)))
                    throw new InvalidDataException("The setup checksum failed. Download the setup again.");
            }
        }
    }

    private static void ExtractPackage(string zip, string directory, IProgress<int> progress)
    {
        string root = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        Directory.CreateDirectory(root);
        using (ZipArchive archive = ZipFile.OpenRead(zip))
        {
            long total = 0;
            foreach (ZipArchiveEntry entry in archive.Entries) { total = checked(total + entry.Length); }
            long completed = 0;
            int lastPercent = -1;
            byte[] buffer = new byte[1024 * 1024];
            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                string target = Path.GetFullPath(Path.Combine(root, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("The installation archive contains an unsafe path.");
                if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                using (Stream source = entry.Open())
                using (FileStream destination = new FileStream(target, FileMode.CreateNew))
                {
                    int count;
                    while ((count = source.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        destination.Write(buffer, 0, count);
                        completed += count;
                        int percent = (int)Math.Min(100L, completed * 100L / Math.Max(1L, total));
                        if (percent != lastPercent) { progress.Report(percent); lastPercent = percent; }
                    }
                }
            }
            progress.Report(100);
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        if (args.Length == 1 && args[0] == "/verify")
        {
            try { CopyPackage(Stream.Null, null); return 0; }
            catch { return 1; }
        }
        string temporary = Path.Combine(Path.GetTempPath(), "LightSetupLauncher-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(temporary);
            string zip = Path.Combine(temporary, "PewVista-Install-Windows-x64.zip");
            string script = Path.Combine(temporary, "setup_windows_self_extract.ps1");
            File.WriteAllBytes(script, Convert.FromBase64String(Bootstrap));
            using (Form progress = new Form())
            {
                progress.Text = "PewVista Setup";
                progress.Font = new System.Drawing.Font("Segoe UI", 10);
                progress.ClientSize = new System.Drawing.Size(560, 230);
                progress.StartPosition = FormStartPosition.CenterScreen;
                progress.FormBorderStyle = FormBorderStyle.FixedDialog;
                progress.ControlBox = false;
                Label label = new Label { Text = "Verifying package: 0%", Left = 20, Top = 20, Width = 520, Height = 130 };
                ProgressBar bar = new ProgressBar { Left = 20, Top = 174, Width = 520, Height = 24, Style = ProgressBarStyle.Continuous };
                progress.Controls.Add(label);
                progress.Controls.Add(bar);
                Exception failure = null;
                progress.Shown += async delegate
                {
                    try
                    {
                        IProgress<int> copyProgress = new Progress<int>(delegate(int percent) { label.Text = "Verifying package: " + percent + "%"; bar.Value = percent; });
                        await Task.Run(delegate
                        {
                            using (FileStream target = File.Create(zip)) { CopyPackage(target, copyProgress); }
                        });
                        string expanded = Path.Combine(temporary, "expanded");
                        label.Text = "Unpacking package: 0%";
                        bar.Value = 0;
                        IProgress<int> extractProgress = new Progress<int>(delegate(int percent) { label.Text = "Unpacking package: " + percent + "%"; bar.Value = percent; });
                        await Task.Run(delegate { ExtractPackage(zip, expanded, extractProgress); });
                        label.Text = "Package unpacked: 100%. Waiting for setup or administrator approval...";
                        bar.Value = 100;
                        ProcessStartInfo start = new ProcessStartInfo();
                        start.FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
                        start.Arguments = "-NoProfile -STA -ExecutionPolicy Bypass -File \"" + script + "\" -ExtractedDirectory \"" + expanded + "\"";
                        start.UseShellExecute = false;
                        start.CreateNoWindow = true;
                        using (Process installer = Process.Start(start))
                        {
                            await Task.Run(delegate { installer.WaitForExit(); });
                            if (installer.ExitCode != 0) throw new InvalidOperationException("Setup did not finish. No successful installation was reported.");
                        }
                    }
                    catch (Exception error) { failure = error; }
                    finally { progress.Close(); }
                };
                Application.Run(progress);
                if (failure != null) throw failure;
            }
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Setup could not finish", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            try { Directory.Delete(temporary, true); } catch { }
        }
    }
}
'@
    $source = $source.Replace("__BOOTSTRAP__", $bootstrap)
    $launcher = Join-Path $workingDirectory "launcher.exe"
    Write-Output "Compiling the Windows setup launcher..."
    Add-Type -TypeDefinition $source -Language CSharp -ReferencedAssemblies System.Windows.Forms,System.Drawing,System.Core,System.IO.Compression,System.IO.Compression.FileSystem -OutputAssembly $launcher -OutputType WindowsApplication
    $candidate = Join-Path $workingDirectory "complete.exe"
    [System.IO.File]::Copy($launcher, $candidate)
    Write-Output "Attaching the frozen Installation ZIP (no recompression)..."
    $inputStream = [System.IO.File]::OpenRead($InstallationArchive)
    $outputStream = [System.IO.File]::Open($candidate, [System.IO.FileMode]::Append)
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        $length = $inputStream.Length
        $buffer = New-Object byte[] (1MB)
        while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            [void]$hash.TransformBlock($buffer, 0, $count, $buffer, 0)
            $outputStream.Write($buffer, 0, $count)
        }
        [void]$hash.TransformFinalBlock([byte[]]@(), 0, 0)
        $footer = [BitConverter]::GetBytes([long]$length) + $hash.Hash + [System.Text.Encoding]::ASCII.GetBytes("TLSETUP1")
        $outputStream.Write($footer, 0, $footer.Length)
    }
    finally { $inputStream.Dispose(); $outputStream.Dispose(); $hash.Dispose() }
    Write-Output "Verifying the complete embedded package checksum..."
    $verification = Start-Process -FilePath $candidate -ArgumentList "/verify" -Wait -PassThru
    if ($verification.ExitCode -ne 0) { throw "Setup verification failed; output was not published." }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sign_windows_file.ps1") -FilePath $candidate
    if ($LASTEXITCODE -ne 0) { throw "Setup.exe could not be code-signed; output was not published." }
    $signedVerification = Start-Process -FilePath $candidate -ArgumentList "/verify" -Wait -PassThru
    if ($signedVerification.ExitCode -ne 0) { throw "The signed Setup.exe failed verification; output was not published." }
    [System.IO.File]::Move($candidate, $SetupExecutable)
    Write-Output "READY: $SetupExecutable"
}
catch { Write-Error $_; exit 1 }
finally {
    if (Test-Path -LiteralPath $workingDirectory) { Remove-Item -LiteralPath $workingDirectory -Recurse -Force }
}