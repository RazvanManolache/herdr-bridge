# Shared private owner/SYSTEM/Administrators ACL for optional paired transport.
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
function Set-PrivateAcl([string]$Path, [bool]$Directory) {
  $allowed = @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')
  $current = Get-Acl -LiteralPath $Path
  $expectedInheritance = if ($Directory) { 'ContainerInherit, ObjectInherit' } else { 'None' }
  $rules = @($current.Access)
  $alreadyPrivate = $current.AreAccessRulesProtected -and $rules.Count -eq 3
  foreach ($rule in $rules) {
    $identity = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($identity -notin $allowed -or $rule.FileSystemRights -ne 'FullControl' -or
        $rule.AccessControlType -ne 'Allow' -or $rule.InheritanceFlags.ToString() -ne $expectedInheritance) {
      $alreadyPrivate = $false
    }
  }
  # Some Windows/PowerShell versions request SACL privileges for an unchanged
  # descriptor. Avoid writing it when its effective private ACL is already exact.
  if ($alreadyPrivate) { return }
  if ($Directory) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
  } else {
    $acl = [Security.AccessControl.FileSecurity]::new()
    $inherit = [Security.AccessControl.InheritanceFlags]::None
  }
  $acl.SetOwner($owner)
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sid in @($owner, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
                    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', $inherit, 'None', 'Allow')
    $acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $Path -AclObject $acl
}
