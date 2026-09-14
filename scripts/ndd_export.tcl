# ndd_export.tcl -- read an OrCAD Capture design and emit wiring + pin function
# names + hierarchy as two CSVs. READ ONLY: never saves, never modifies the design.
#
#   tclsh.exe ndd_export.tcl <design.DSN> [outdir]
#
# The design must NOT be open in Capture (Capture holds a .DSNlck lock).

fconfigure stdout -buffering none

# Derive the DLL from the interpreter that is running us: tclsh.exe always
# lives next to orDb_Dll_Tcl64.dll inside <SPB root>/tools/bin.  Hardcoding a
# version (SPB_22.1) would break on every other install, and the Python side
# already picks the newest SPB it finds -- the two must not disagree.
# NDD_ORDB_DLL overrides, for non-standard layouts.
if {[info exists env(NDD_ORDB_DLL)]} {
    set DLL $env(NDD_ORDB_DLL)
} else {
    set DLL [file join [file dirname [info nameofexecutable]] orDb_Dll_Tcl64]
}
if {[catch {load $DLL DboTclWriteBasic} e]} {
    puts "ERROR: cannot load $DLL"
    puts "  $e"
    puts "  (expected it next to [info nameofexecutable];"
    puts "   set NDD_ORDB_DLL to override)"
    exit 1
}

proc sval {c} { return [DboTclHelper_sGetConstCharPtr $c] }

# call a getter that fills a CString; empty string if the overload is unavailable
proc getstr {obj m} {
    set c [DboTclHelper_sMakeCString]
    if {[catch {$obj $m $c}]} { return "" }
    return [sval $c]
}

# read a named Capture property off an occurrence
proc getprop {occ name} {
    set n [DboTclHelper_sMakeCString $name]
    set v [DboTclHelper_sMakeCString]
    if {[catch {$occ GetEffectivePropStringValue $n $v}]} { return "" }
    return [sval $v]
}

proc csvq {s} {
    if {[string match {*[,"]*} $s] || [string match "*\n*" $s]} {
        return "\"[string map {\" \"\"} $s]\""
    }
    return $s
}

# ---------------------------------------------------------------- args
if {[llength $argv] < 1} {
    puts "usage: tclsh ndd_export.tcl <design.DSN> \[outdir\]"
    exit 1
}
set dsn [file normalize [lindex $argv 0]]
if {![file exists $dsn]} { puts "ERROR: no such file: $dsn"; exit 1 }
set outdir [expr {[llength $argv] > 1 ? [lindex $argv 1] : [file dirname $dsn]}]
file mkdir $outdir
set stem [file rootname [file tail $dsn]]

if {[file exists "$dsn\lck"]} {
    puts "WARNING: [file tail $dsn]lck exists -- the design may be open in Capture."
    puts "         Close it, or this run will block."
}

# ---------------------------------------------------------------- open
set mode $::IterDefs_ALL
set pSession [DboTclHelper_sCreateSession]
set pStatus  [DboState]
set pDesign  [$pSession GetDesignAndSchematics [DboTclHelper_sMakeCString $dsn] $pStatus]
if {$pDesign == "NULL"} { puts "ERROR: could not open design"; exit 1 }
puts "opened : $dsn"
if {[$pDesign IsRootOccurrenceExisting] == 0} { $pDesign GetRootOccurrence $pStatus }

set reuse 0
catch { set reuse [$pDesign DesignHasReusedSchematics] }
set occprop 0
catch { set occprop [$pDesign DesignHasOccurrenceProperties $pStatus] }
puts "mode   : reused-schematics=$reuse occurrence-properties=$occprop"

# ---------------------------------------------------------------- 1. parts
set pf [open [file join $outdir "${stem}_parts.csv"] w]
fconfigure $pf -translation lf
puts $pf "id,parent_id,depth,refdes,base_refdes,inst_path,source_part,value,footprint,is_block,netlist_ignore,hier_path_display"
set nparts 0
set nblocks 0
set nign 0
# inst_path -> id, so nodes can be linked back without ever splitting a path
array set IDOF {}
if {[catch {
    DboDesignOccurrencesIter oit $pDesign
    set occ [oit NextOccurrence $pStatus]
    while {$occ != "NULL"} {
        set ref  [getstr $occ GetReferenceDesignator]
        set hier [getstr $occ GetRefPathName]
        set ipath [getstr $occ GetPathName]

        # Structure comes from the API, never from parsing the path string:
        # block and pin names legitimately contain "/", " ", "+", "#", "-" and
        # backslashes, so no delimiter is safe to split on.
        set id ""; catch { set id [$occ GetId $pStatus] }
        set dep ""; catch { set dep [$occ GetDepth $pStatus] }
        set par ""
        if {![catch { set pobj [$occ GetParent] }] && $pobj ne "NULL" && $pobj ne ""} {
            if {![catch { set pocc [DboBaseObjectToDboOccurrence $pobj] }] && $pocc ne "NULL"} {
                catch { set par [$pocc GetId $pStatus] }
            }
        }
        set isblk 0
        if {![catch { set pi [$occ GetPartInst $pStatus] }] && $pi != "NULL"} {
            if {![catch { set prim [$pi IsPrimitive $pStatus] }] && $prim == 0} { set isblk 1 }
        }
        set pname [getprop $occ "Source Part"]
        set val   [getprop $occ "Value"]
        set fp    [getprop $occ "PCB Footprint"]
        # Objects PADS excludes from the netlist. Capture's IsNetlistIgnore does NOT
        # mean this (it returns 0 for the board title symbol), so key off the
        # footprint the library uses for it. Verified on 4 boards: hits PCB1 only,
        # zero false positives. A "no pins" rule was tried and rejected -- it also
        # matches fiducials and mounting holes, which PADS *does* list.
        set ign [expr {$fp eq "PCB_LABEL" ? 1 : 0}]
        if {$ref ne "" || $hier ne ""} {
            # Multi-section parts carry a section suffix (U1-1, J4A); the netlist
            # uses the base refdes. Real part refdes never contain "/", so the last
            # segment is safe for them -- but BLOCK names do (SPST_P/R_8), so leave
            # base_refdes empty for blocks rather than emit a wrong value. Use
            # id/parent_id for block structure instead.
            set bref ""
            if {!$isblk} { set bref [lindex [split $ipath "/"] end] }
            set IDOF($ipath) $id
            puts $pf "$id,$par,$dep,[csvq $ref],[csvq $bref],[csvq $ipath],[csvq $pname],[csvq $val],[csvq $fp],$isblk,$ign,[csvq $hier]"
            if {$isblk} { incr nblocks } elseif {$ign != 1} { incr nparts } else { incr nign }
        }
        set occ [oit NextOccurrence $pStatus]
    }
} e]} { puts "WARN parts: $e" }
close $pf

# ---------------------------------------------------------------- 2. nodes
set nf [open [file join $outdir "${stem}_nodes.csv"] w]
fconfigure $nf -translation lf
puts $nf "net,is_global,is_power,owner_id,refdes,pin_number,pin_name,kind,inst_path_display"
set nnets 0
set nnodes 0
set nports 0
set nnamed 0
if {[catch {
    DboDesignFlatNetsIter nit $pDesign $mode
    set net [nit NextFlatNet $pStatus]
    while {$net != "NULL"} {
        incr nnets
        set nm [getstr $net GetName]
        set g 0; catch { set g [$net GetIsGlobal $pStatus] }
        set p 0; catch { set p [$net GetIsPower $pStatus] }
        if {![catch { set pit [$net NewPortOccurrencesIter $pStatus $mode] }] && $pit != "NULL"} {
            set po [$pit NextPortOccurrence $pStatus]
            while {$po != "NULL"} {
                set num   [getstr $po GetPinNumber]
                set pn    [getstr $po GetPinName]
                set rpath [getstr $po GetRefPathName]
                set ipath [getstr $po GetPathName]
                set kind [expr {$num eq "" ? "port" : "pin"}]
                if {$kind eq "pin"} { incr nnodes } else { incr nports }
                if {$pn ne "" && $pn ne $num} { incr nnamed }
                # inst_path ends with the pin name, which may itself contain "/"
                # (RT/CT, SLOW/FAST) -- strip it by length, never by split.
                set base $ipath
                if {$pn ne ""} {
                    set suf "/$pn"
                    set L [string length $suf]
                    if {[string range $ipath end-[expr {$L-1}] end] eq $suf} {
                        set base [string range $ipath 0 end-$L]
                    }
                }
                # $base is now the owning occurrence's inst_path -- use it whole as
                # a lookup key (never split) to recover that row's id.
                set oid ""
                if {[info exists IDOF($base)]} { set oid $IDOF($base) }
                set rd [lindex [split $base "/"] end]
                puts $nf "[csvq $nm],$g,$p,$oid,[csvq $rd],[csvq $num],[csvq $pn],$kind,[csvq $ipath]"
                set po [$pit NextPortOccurrence $pStatus]
            }
        }
        set net [nit NextFlatNet $pStatus]
    }
} e]} { puts "WARN nets: $e" }
close $nf

# ---------------------------------------------------------------- done
puts "parts  : $nparts   blocks: $nblocks   netlist-ignored: $nign"
puts "nets   : $nnets   pins: $nnodes   hier-ports: $nports   named-pins: $nnamed"
puts "wrote  : [file join $outdir ${stem}_parts.csv]"
puts "         [file join $outdir ${stem}_nodes.csv]"

catch { $pSession RemoveDesign $pDesign $pStatus }
catch { DboTclHelper_sReleaseAllCreatedPtrs }
catch { DboTclHelper_sDeleteSession $pSession }
puts "done"
