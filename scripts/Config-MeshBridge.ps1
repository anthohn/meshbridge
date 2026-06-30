<#
.SYNOPSIS
    MeshBridge — Déploiement et vérification de nœuds Meshtastic
    Normes Netiquette Suisse (janvier 2026) + canal privé chiffré.

.DESCRIPTION
    Configure un nœud Meshtastic de façon reproductible et VÉRIFIÉE :
    chaque paramètre critique est relu après écriture et comparé à la
    valeur attendue. Le script ne déclare le succès que si toutes les
    vérifications passent.

    Points clés Netiquette janvier 2026 :
      - Preset MEDIUM_FAST (le mesh suisse a quitté LONG_FAST en 2025).
      - Position : 24h pour les nœuds fixes, 6h pour les mobiles.
      - Rôle CLIENT_MUTE (zone dense / nœud transporté).
      - Hop limit 3, duty cycle respecté, MQTT ignoré.

.NOTES
    Le preset (modem_preset) est appliqué SÉPARÉMENT : groupé avec
    use_preset dans une même transaction, il est silencieusement ignoré
    (constaté sur firmware 2.7.9).

.EXAMPLE
    .\Config-MeshBridge.ps1
    Lance le menu interactif (déployer Maison / Portable / vérifier).
#>

#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ======================================================================
#  CHARGEMENT DU .env (à la racine du repo, à côté de README.md)
# ======================================================================
# Lit le fichier .env et exporte chaque ligne KEY=VALUE comme variable
# d'environnement pour ce processus. Aucun secret n'est donc jamais
# écrit en dur dans ce script — et .env est ignoré par Git (.gitignore).
$Script:EnvPath = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"

if (Test-Path $Script:EnvPath) {
    Get-Content $Script:EnvPath | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { return }
        $key   = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
    Write-Host "[.env] Variables chargées depuis $Script:EnvPath" -ForegroundColor DarkGray
} else {
    Write-Host "[!] .env introuvable à la racine du repo ($Script:EnvPath)" -ForegroundColor Yellow
    Write-Host "    Copie .env.example en .env et remplis tes clés." -ForegroundColor Yellow
}

# ======================================================================
#  CONFIGURATION GLOBALE
# ======================================================================
$Script:Config = @{
    PSK         = $env:MESHBRIDGE_PSK
    Channel     = 1         # index du canal privé MeshBridge
    MinFirmware = "2.7.0"   # version minimale recommandée

    # Noms NEUTRES diffusés dans le NodeInfo public (toutes les 3h, en clair).
    # Évite tout mot révélateur (maison, pont, gateway, portable, relai...).
    # Long = nom long affiché ; Short = nom court (max 4 caractères).
    # Change-les pour ce que tu veux, du moment que ça ne dévoile pas l'usage.
    Nodes = @{
        Maison   = @{ Long = "Aurora";  Short = "AUR" }
        Portable = @{ Long = "Nimbus";  Short = "NIM" }
    }
}

# Valeurs numériques renvoyées par "meshtastic --get" (enums protobuf),

# pour traduire les codes en libellés lisibles lors de la vérification.
$Script:Enums = @{
    "lora.region"             = @{ "3" = "EU_868" }
    "lora.modem_preset"       = @{ "0" = "LONG_FAST"; "4" = "MEDIUM_FAST" }
    "device.role"             = @{ "0" = "CLIENT"; "1" = "CLIENT_MUTE" }
    "device.rebroadcast_mode" = @{ "0" = "ALL"; "2" = "LOCAL_ONLY" }
}

# ======================================================================
#  UTILITAIRES
# ======================================================================

function Write-Step  { param($m) Write-Host "  -> $m" -ForegroundColor DarkGray }
function Write-Ok    { param($m) Write-Host "  [OK]   $m" -ForegroundColor Green }
function Write-Fail  { param($m) Write-Host "  [ÉCHEC] $m" -ForegroundColor Red }
function Write-Title { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

function Invoke-Meshtastic {
    <#  Exécute la CLI meshtastic, capture stdout+stderr, et lève une
        exception explicite si le code de sortie est non nul.  #>
    param([string[]]$Arguments)

    $output = & meshtastic @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "meshtastic $($Arguments -join ' ') a échoué (code $LASTEXITCODE) :`n$output"
    }
    return $output
}

function Get-Setting {
    <#  Lit un champ de config et renvoie sa valeur brute (sans le bruit
        'Connected to radio' / 'Completed getting preferences').  #>
    param([string]$Field)

    $raw = Invoke-Meshtastic @("--get", $Field)
    foreach ($line in $raw) {
        if ($line -match "^\s*$([regex]::Escape($Field))\s*:\s*(.+?)\s*$") {
            return $Matches[1].Trim()
        }
    }
    throw "Impossible de lire la valeur de '$Field'."
}

function Test-Setting {
    <#  Vérifie qu'un champ vaut bien la valeur attendue. Traduit les
        enums numériques. Renvoie $true/$false et journalise le résultat.  #>
    param(
        [string]$Field,
        [string]$Expected,        # valeur attendue (libellé ou nombre)
        [string]$Label = $Field
    )

    $actual = Get-Setting -Field $Field

    # Traduction enum éventuelle pour comparer des libellés lisibles
    $actualLabel = $actual
    if ($Script:Enums.ContainsKey($Field) -and $Script:Enums[$Field].ContainsKey($actual)) {
        $actualLabel = $Script:Enums[$Field][$actual]
    }

    if ($actualLabel -eq $Expected -or $actual -eq $Expected) {
        Write-Ok ("{0,-22} = {1}" -f $Label, $actualLabel)
        return $true
    } else {
        Write-Fail ("{0,-22} = {1}  (attendu : {2})" -f $Label, $actualLabel, $Expected)
        return $false
    }
}

# ======================================================================
#  PRÉREQUIS
# ======================================================================

function Test-Prerequisites {
    Write-Title "Vérification des prérequis..."

    if ([string]::IsNullOrWhiteSpace($Script:Config.PSK)) {
        Write-Fail "MESHBRIDGE_PSK manquant ou vide."
        Write-Host "  Vérifie que .env existe à la racine du repo et contient MESHBRIDGE_PSK=..." -ForegroundColor Yellow
        exit 1
    }

    if (-not (Get-Command meshtastic -ErrorAction SilentlyContinue)) {
        Write-Fail "CLI 'meshtastic' introuvable. Installe-la : pip install meshtastic"
        exit 1
    }

    try {
        $version = (Invoke-Meshtastic @("--version") | Select-Object -First 1).Trim()
        Write-Ok "Nœud détecté — firmware/CLI $version"
        if ([version]($version -replace '[^\d.]','') -lt [version]$Script:Config.MinFirmware) {
            Write-Host "  [!] Firmware < $($Script:Config.MinFirmware) : certains réglages peuvent être indisponibles." -ForegroundColor Yellow
        }
    } catch {
        Write-Fail "Aucun nœud accessible. Branche-le en USB.`n$_"
        exit 1
    }
}

# ======================================================================
#  APPLICATION DE LA CONFIGURATION
# ======================================================================

function Set-CommonSettings {
    param(
        [string]$LongName,
        [string]$ShortName
    )
    Write-Title "[1/3] Identité et paramètres communs ($LongName / $ShortName)"

    Invoke-Meshtastic @("--set-owner", $LongName, "--set-owner-short", $ShortName) | Out-Null

    Write-Step "Écriture LoRa / réseau suisse / télémétrie..."
    Invoke-Meshtastic @(
        "--set", "lora.region", "EU_868",
        "--set", "lora.use_preset", "true",
        "--set", "lora.hop_limit", "3",
        "--set", "lora.override_duty_cycle", "false",
        "--set", "lora.ignore_mqtt", "true",
        "--set", "device.node_info_broadcast_secs", "10800",
        "--set", "position.position_broadcast_smart_enabled", "false",
        "--set", "telemetry.device_update_interval", "259200",
        "--set", "telemetry.environment_measurement_enabled", "false",
        "--set", "telemetry.power_measurement_enabled", "false",
        "--set", "mqtt.enabled", "false"
    ) | Out-Null

    # Preset appliqué SÉPARÉMENT (sinon ignoré, cf. .NOTES)
    Write-Step "Application du preset MEDIUM_FAST (transaction isolée)..."
    Invoke-Meshtastic @("--set", "lora.modem_preset", "MEDIUM_FAST") | Out-Null
}

function Set-RoleSettings {
    param(
        [ValidateSet("Maison", "Portable")] [string]$Type
    )
    if ($Type -eq "Maison") {
        Write-Title "[2/3] Règles MAISON (fixe / position 24h)"
        $posSecs = "86400"; $fixed = "true"
    } else {
        Write-Title "[2/3] Règles PORTABLE (mobile / position 6h)"
        $posSecs = "21600"; $fixed = "false"
    }

    Invoke-Meshtastic @(
        "--set", "device.role", "CLIENT_MUTE",
        "--set", "device.rebroadcast_mode", "LOCAL_ONLY",
        "--set", "position.position_broadcast_secs", $posSecs,
        "--set", "position.fixed_position", $fixed
    ) | Out-Null
}

function Set-PrivateChannel {
    Write-Title "[3/3] Canal privé chiffré MeshBridge (index $($Script:Config.Channel))"
    Invoke-Meshtastic @(
        "--ch-index", "$($Script:Config.Channel)",
        "--ch-set", "name", "MeshBridge",
        "--ch-set", "psk", "base64:$($Script:Config.PSK)",
        "--ch-set", "uplink_enabled", "false",
        "--ch-set", "downlink_enabled", "false"
    ) | Out-Null
}

# ======================================================================
#  VÉRIFICATION POST-DÉPLOIEMENT
# ======================================================================

function Test-Deployment {
    param(
        [ValidateSet("Maison", "Portable")] [string]$Type
    )
    Write-Title "Vérification post-déploiement (relecture réelle des champs)"

    $posExpected = if ($Type -eq "Maison") { "86400" } else { "21600" }

    $results = @(
        (Test-Setting "lora.region"                              "EU_868"      "Région"),
        (Test-Setting "lora.modem_preset"                        "MEDIUM_FAST" "Preset LoRa"),
        (Test-Setting "lora.hop_limit"                           "3"           "Hop limit"),
        (Test-Setting "device.role"                              "CLIENT_MUTE" "Rôle"),
        (Test-Setting "device.rebroadcast_mode"                  "LOCAL_ONLY"  "Rebroadcast"),
        (Test-Setting "position.position_broadcast_smart_enabled" "False"      "Smart position"),
        (Test-Setting "position.position_broadcast_secs"         $posExpected  "Position interval")
    )

    $passed = ($results | Where-Object { $_ }).Count
    $total  = $results.Count

    Write-Host ""
    if ($passed -eq $total) {
        Write-Host "  ✅ $passed/$total vérifications réussies — nœud $Type conforme." -ForegroundColor Green
        return $true
    } else {
        Write-Host "  ❌ $passed/$total réussies — $($total - $passed) à corriger ci-dessus." -ForegroundColor Red
        return $false
    }
}

function Show-ChannelUrl {
    Write-Title "URL des canaux (doit être IDENTIQUE sur les deux nœuds)"
    Write-Host "  Compare cette ligne entre Maison et Portable : PSK partagé = OK" -ForegroundColor DarkGray
    Invoke-Meshtastic @("--info") | Select-String -Pattern "Complete URL" -Context 0,1
}

# ======================================================================
#  ORCHESTRATION
# ======================================================================

function Deploy-Node {
    param(
        [ValidateSet("Maison", "Portable")] [string]$Type
    )
    $node = $Script:Config.Nodes[$Type]
    $name = $node.Long

    try {
        Set-CommonSettings -LongName $node.Long -ShortName $node.Short
        Set-RoleSettings   -Type $Type
        Set-PrivateChannel

        $ok = Test-Deployment -Type $Type
        Show-ChannelUrl

        Write-Host ""
        if ($ok) {
            Write-Host "════════════════════════════════════════════════════" -ForegroundColor Green
            Write-Host "  ✅ Nœud '$name' déployé ET vérifié." -ForegroundColor Green
            Write-Host "════════════════════════════════════════════════════" -ForegroundColor Green
        } else {
            Write-Host "════════════════════════════════════════════════════" -ForegroundColor Red
            Write-Host "  ⚠ Nœud '$name' déployé mais NON conforme — voir échecs." -ForegroundColor Red
            Write-Host "════════════════════════════════════════════════════" -ForegroundColor Red
        }
    } catch {
        Write-Host ""
        Write-Fail "Déploiement interrompu : $_"
    }
}

# ======================================================================
#  MENU INTERACTIF
# ======================================================================

function Show-Menu {
    Clear-Host
    Write-Host "=================================================" -ForegroundColor Cyan
    Write-Host "   GESTIONNAIRE DE DÉPLOIEMENT MESHBRIDGE" -ForegroundColor White
    Write-Host "   Netiquette Suisse - Janvier 2026" -ForegroundColor DarkGray
    Write-Host "=================================================" -ForegroundColor Cyan
    Write-Host " 1. Déployer + vérifier le Pont Maison (Heltec v3)"
    Write-Host " 2. Déployer + vérifier le Nœud Portable (LilyGO)"
    Write-Host " 3. Vérifier seulement le nœud branché"
    Write-Host " 4. Quitter"
    Write-Host "-------------------------------------------------"
}

Test-Prerequisites
Read-Host "`nPrérequis OK. Appuie sur Entrée pour continuer"

do {
    Show-Menu
    $choix = Read-Host "Sélectionne une option (1-4)"

    switch ($choix) {
        '1' { Deploy-Node -Type "Maison";   Read-Host "`nEntrée pour revenir au menu" }
        '2' { Deploy-Node -Type "Portable"; Read-Host "`nEntrée pour revenir au menu" }
        '3' {
            try {
                # Type déduit de l'intervalle de position lu sur le nœud
                $pos = Get-Setting "position.position_broadcast_secs"
                $type = if ($pos -eq "86400") { "Maison" } else { "Portable" }
                Write-Host "  (nœud détecté comme : $type, position=$pos s)" -ForegroundColor DarkGray
                Test-Deployment -Type $type | Out-Null
                Show-ChannelUrl
            } catch {
                Write-Fail $_
            }
            Read-Host "`nEntrée pour revenir au menu"
        }
        '4' { Write-Host "Fermeture..."; break }
        default { Write-Host "Option invalide." -ForegroundColor Red; Start-Sleep -Seconds 1 }
    }
} while ($choix -ne '4')
