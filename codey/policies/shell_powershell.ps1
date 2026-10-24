param([Parameter(Mandatory = $true)][string]$SourceBase64)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($SourceBase64))
$tokens = $null
$parseErrors = $null
# ParseInput constructs an AST. Never compile or invoke the supplied source.
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
$all = @($ast.FindAll({ param($node) $true }, $true))
if ($all.Count -gt 10000) { throw 'PowerShell syntax node budget exceeded' }
$commands = @()
$redirects = @()
$expansions = @()
$operators = @()
$unknown = @()
$dynamic = $false
$known = @('ScriptBlockAst', 'NamedBlockAst', 'PipelineAst', 'CommandAst', 'CommandExpressionAst',
    'StringConstantExpressionAst', 'ConstantExpressionAst', 'CommandParameterAst', 'VariableExpressionAst',
    'ExpandableStringExpressionAst', 'FileRedirectionAst', 'MergingRedirectionAst', 'SubExpressionAst',
    'StatementBlockAst', 'ParenExpressionAst')
foreach ($node in $all) {
    $type = $node.GetType().Name
    if ($known -notcontains $type) { $unknown += $type }
    if ($type -in @('VariableExpressionAst', 'SubExpressionAst')) { $expansions += $node.Extent.Text }
    if ($type -in @('SubExpressionAst', 'InvokeMemberExpressionAst', 'ScriptBlockExpressionAst')) { $dynamic = $true }
    if ($type -eq 'PipelineAst' -and $node.PipelineElements.Count -gt 1) { $operators += '|' }
    if ($type -eq 'CommandAst') {
        $words = @()
        foreach ($element in $node.CommandElements) {
            $literal = $null
            $wordExpansions = @($element.FindAll({ param($n)
                $n -is [System.Management.Automation.Language.VariableExpressionAst] -or
                $n -is [System.Management.Automation.Language.SubExpressionAst]
            }, $true) | ForEach-Object { $_.Extent.Text })
            switch ($element.GetType().Name) {
                'StringConstantExpressionAst' { $literal = $element.Value }
                'ConstantExpressionAst' { if ($null -ne $element.Value) { $literal = [string]$element.Value } }
                'ExpandableStringExpressionAst' { if ($element.NestedExpressions.Count -eq 0) { $literal = $element.Value } }
                'CommandParameterAst' { if ($null -eq $element.Argument) { $literal = $element.Extent.Text } }
            }
            $words += @{ raw = $element.Extent.Text; literal = $literal; expansions = @($wordExpansions) }
        }
        if ($node.InvocationOperator.ToString() -ne 'Unknown') { $dynamic = $true }
        $commands += @{ words = @($words) }
    }
    if ($type -in @('FileRedirectionAst', 'MergingRedirectionAst')) {
        $target = $null
        $operator = $node.Extent.Text
        if ($type -eq 'FileRedirectionAst') {
            $raw = $node.Location.Extent.Text
            $literal = $null
            if ($node.Location -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                $literal = $node.Location.Value
            }
            $target = @{ raw = $raw; literal = $literal; expansions = @() }
            $operator = $operator.Substring(0, $operator.Length - $raw.Length).Trim()
        }
        $redirects += @{ operator = $operator; target = $target }
    }
}
foreach ($token in $tokens) {
    if ($token.Kind.ToString() -in @('Semi', 'NewLine', 'AndAnd', 'OrOr', 'Ampersand')) {
        $operators += $token.Text
    }
}
$reason = ''
$status = 'parsed'
if (@($parseErrors).Count -gt 0) {
    $status = 'invalid'
    $reason = 'PowerShell syntax error: ' + $parseErrors[0].Message
} elseif ($unknown.Count -gt 0) {
    $status = 'opaque'
    $reason = 'unsupported syntax: ' + (($unknown | Sort-Object -Unique) -join ', ')
}
@{ commands = @($commands); operators = @($operators); redirections = @($redirects);
   expansions = @($expansions); dynamic = [bool]$dynamic; status = $status; reason = $reason } |
    ConvertTo-Json -Depth 12 -Compress
