# Read-only inventory query. Uses only Get-* cmdlets.
$ErrorActionPreference = 'Stop'
$cs   = Get-CimInstance -ClassName Win32_ComputerSystem
$bios = Get-CimInstance -ClassName Win32_BIOS
$enc  = Get-CimInstance -ClassName Win32_SystemEnclosure | Select-Object -First 1
$os   = Get-CimInstance -ClassName Win32_OperatingSystem
$csp  = Get-CimInstance -ClassName Win32_ComputerSystemProduct
$adapters = @(Get-NetAdapter -IncludeHidden:$false | ForEach-Object {
    [ordered]@{
        name        = $_.Name
        description = $_.InterfaceDescription
        mac         = $_.MacAddress
        status      = [string]$_.Status
        virtual     = [bool]$_.Virtual
        ifindex     = $_.ifIndex
    }
})
$ips = @(Get-NetIPAddress | Where-Object { $_.AddressState -eq 'Preferred' } | ForEach-Object {
    [ordered]@{ ifalias = $_.InterfaceAlias; ifindex = $_.InterfaceIndex; address = $_.IPAddress;
                prefix = $_.PrefixLength; family = [string]$_.AddressFamily }
})
$routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    ForEach-Object { [ordered]@{ ifindex = $_.InterfaceIndex; nexthop = $_.NextHop } })
[ordered]@{
    hostname      = $env:COMPUTERNAME
    dns_domain    = $cs.Domain
    manufacturer  = $cs.Manufacturer
    model         = $cs.Model
    serial        = $bios.SerialNumber
    asset_tag     = $enc.SMBIOSAssetTag
    uuid          = $csp.UUID
    os_caption    = $os.Caption
    os_version    = $os.Version
    os_build      = $os.BuildNumber
    hypervisor_present = [bool]$cs.HypervisorPresent
    adapters      = $adapters
    ip_addresses  = $ips
    default_routes = $routes
}
