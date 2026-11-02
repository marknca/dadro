; $Log: m_tools.inc,v $
; Revision 1.1  2006/11/01 21:54:29  nickm
; Initial revision
;
; m_tools.inc
; version 0.2 / English
; *************************************************************************
; *** Tool-geometry (mills and their radii with Z-offset                ***
; ***                                                                   ***
; *** TGZ stands for the Z-offset (TG = Tool Geometry)                  ***
; *** TGR stands for the mill's radius                                  ***
; *************************************************************************
Interp:
  fvar TGZ, TGR;
  fvar TGActive;  // flag to signal that a tool is active. Needed for editing
	fvar TGXYMoveX, TGXYMoveY;
:EndI
;
; tools start with a "#". This way, they get sortet into a different list.
; This list is the tools-list that can be reached by pressing the "#" key
; while DRO:ddisp is running.
; I have put all the tools into a separate file (this file).
; It gets included by:
;"Include:m_tools.inc" in the yadro.cfg-file.
;
; The macros presented here relay on the TGxx-Macros in the MILLx.CFG-file.
; They also relay on the macro "internalToolGeometry" that can also be found
; in yadro.cfg
;
; ---------------------------------------
Macro:#- deactivate tool geometry
  TGZ = 0; TGR = 0;
  TGActive = 0;

  execmacro("internalTGOFF");
  display("ShowTool", "");
:EndM
; ----------------------------------------
Macro:#E Edit current tool
  if (TGActive == 0) {
    input("No tool active!");
  }
  else {
    if (input("Edit current tool",
              "Tool radius:", TGR,
              "Z-offset:", TGZ)) {
      // save tools in base unit (mm)

      TGR = TGR / UnitScaling;
      TGZ = TGZ / UnitScaling;

      perswrite(TGR);
      perswrite(TGZ);
      
      // undo scaling
      TGR = TGR * UnitScaling;
      TGZ = TGZ * UnitScaling;
    }
  }
:EndM
; ----------------------------------------
Macro:internalTGActivate
  TGActive = 1;

  // ---- rescale tools. They are stored in mm (or the reference unit)
  //      and need to be scaled back in the moment they are activated
  //      The macros TGC* can't (currently) handle a change in units while
  //      correction is active. *BUT* we can handle tools properly in mm/inch
  TGR = TGR * UnitScaling;
  TGZ = TGZ * UnitScaling;

  execmacro("internalToolGeometry");
:EndM
; ----------------------------------------
; *******************************************************************************
; *** If you want to add more tools, copy a macro and pay attention that the  ***
; *** persread (2*), ShowTool and the macro-name match (4 places to           ***
; *** make adjustments)                                                       ***
; ***                                                                         ***
; ***           ! NO BLANKS IN THE SECOND PARAMETER OF 'persread' !           ***
; ***                                                                         ***
; *******************************************************************************
Macro:#00
  persread(TGZ, "ToolGeometry00TGZ");
  persread(TGR, "ToolGeometry00TGR");

  display("ShowTool", "#00");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#01
  persread(TGZ, "ToolGeometry01TGZ");
  persread(TGR, "ToolGeometry01TGR");

  display("ShowTool", "#01");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#02
  persread(TGZ, "ToolGeometry02TGZ");
  persread(TGR, "ToolGeometry02TGR");

  display("ShowTool", "#02");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#03
  persread(TGZ, "ToolGeometry03TGZ");
  persread(TGR, "ToolGeometry03TGR");

  display("ShowTool", "#03");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#04
  persread(TGZ, "ToolGeometry04TGZ");
  persread(TGR, "ToolGeometry04TGR");

  display("ShowTool", "#04");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#05
  persread(TGZ, "ToolGeometry05TGZ");
  persread(TGR, "ToolGeometry05TGR");

  display("ShowTool", "#05");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#06
  persread(TGZ, "ToolGeometry06TGZ");
  persread(TGR, "ToolGeometry06TGR");

  display("ShowTool", "#06");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#07
  persread(TGZ, "ToolGeometry07TGZ");
  persread(TGR, "ToolGeometry07TGR");

  display("ShowTool", "#07");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#08
  persread(TGZ, "ToolGeometry08TGZ");
  persread(TGR, "ToolGeometry08TGR");

  display("ShowTool", "#08");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#09
  persread(TGZ, "ToolGeometry09TGZ");
  persread(TGR, "ToolGeometry09TGR");

  display("ShowTool", "#09");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#10
  persread(TGZ, "ToolGeometry10TGZ");
  persread(TGR, "ToolGeometry10TGR");

  display("ShowTool", "#10");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------

