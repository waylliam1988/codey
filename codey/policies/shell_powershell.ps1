param([Parameter(Mandatory = $true)][string]$SourceBase64, [switch]$PrepareTrailingQuote)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($SourceBase64))
$tokens = $null
$parseErrors = $null
# ParseInput constructs an AST. Never compile or invoke the supplied source.
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
# Repair only an isolated erroneous quote at the end. Offsets remain UTF-16
# inside the engine that produced them; neither parse invokes the source.
if ($PrepareTrailingQuote -and @($parseErrors).Count -gt 0) {
    $position = $source.TrimEnd().Length - 1
    $quoteTokens = @($tokens | Where-Object {
        $_.Kind -eq [System.Management.Automation.Language.TokenKind]::StringExpandable -and
        ($_.TokenFlags -band [System.Management.Automation.Language.TokenFlags]::TokenInError) -and
        $_.Text -ceq '"' -and $_.Extent.StartOffset -eq $position -and $_.Extent.EndOffset -eq ($position + 1)
    })
    $terminators = @($parseErrors | Where-Object { $_.ErrorId -eq 'TerminatorExpectedAtEndOfString' })
    $otherErrors = @($parseErrors | Where-Object {
        $_.ErrorId -notin @('TerminatorExpectedAtEndOfString', 'UnexpectedToken') -or
        $_.Extent.StartOffset -ne $position -or $_.Extent.EndOffset -ne ($position + 1)
    })
    if ($quoteTokens.Count -eq 1 -and $terminators.Count -gt 0 -and $otherErrors.Count -eq 0) {
        $candidate = $source.Remove($position, 1)
        $candidateTokens = $null
        $candidateErrors = $null
        $candidateAst = [System.Management.Automation.Language.Parser]::ParseInput(
            $candidate, [ref]$candidateTokens, [ref]$candidateErrors)
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and @($candidateErrors).Count -eq 0) {
            $source = $candidate
            $ast = $candidateAst
            $tokens = $candidateTokens
            $parseErrors = $candidateErrors
        }
    }
}
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
function Get-ShellWord($element) {
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
    # Cmdlets can interpret wildcard strings even when quoted. A literal
    # AST value is not proof that the resolved command treats it literally.
    if ($null -ne $literal -and [System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($literal)) {
        $wordExpansions += $element.Extent.Text
    }
    return @{ raw = $element.Extent.Text; literal = $literal; expansions = @($wordExpansions) }
}
foreach ($node in $all) {
    $type = $node.GetType().Name
    if ($known -notcontains $type) { $unknown += $type }
    if ($type -in @('VariableExpressionAst', 'SubExpressionAst')) { $expansions += $node.Extent.Text }
    if ($type -in @('SubExpressionAst', 'InvokeMemberExpressionAst', 'ScriptBlockExpressionAst')) { $dynamic = $true }
    if ($type -eq 'PipelineAst' -and $node.PipelineElements.Count -gt 1) { $operators += '|' }
    if ($type -eq 'CommandAst') {
        $words = @()
        foreach ($element in $node.CommandElements) {
            $word = Get-ShellWord $element
            $words += $word
            $expansions += $word.expansions
        }
        if ($node.InvocationOperator.ToString() -ne 'Unknown') { $dynamic = $true }
        $commands += @{ words = @($words) }
    }
    if ($type -in @('FileRedirectionAst', 'MergingRedirectionAst')) {
        $target = $null
        $operator = $node.Extent.Text
        if ($type -eq 'FileRedirectionAst') {
            $raw = $node.Location.Extent.Text
            $target = Get-ShellWord $node.Location
            $expansions += $target.expansions
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
@{ command = $source; commands = @($commands); operators = @($operators); redirections = @($redirects);
   expansions = @($expansions); dynamic = [bool]$dynamic; status = $status; reason = $reason } |
    ConvertTo-Json -Depth 12 -Compress
