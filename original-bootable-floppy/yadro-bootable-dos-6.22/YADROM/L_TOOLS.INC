; l_tools.inc
; version 0.2 / English
; *************************************************************************
; *** Tool-geometry (mills and their radii with Z-offset                ***
; ***                                                                   ***
; *** TGZ stands for the Z-offset (TG = Tool Geometry)                  ***
; *** TGR stands for the mill's radius                                  ***
; *************************************************************************
Interp:
  fvar TGZ, TTR, TGW, TGX;  // Z-Offset, tip radius, tool width, X-offset
  fvar TGActive;  // flag to signal that a tool is active. Needed for editing
:EndI
;
; tools start with a "#". This way, they get sortet into a different list.
; This list is the tools-list that can be reached by pressing the "#" key
; while DRO:ddisp is running.
; I have put all the tools into a separate file (this file).
; It gets included by:
;"Include:m_tools.inc" in the yadro.cfg-file.
;
; The macros presented here relay on the TGxx-Macros in the LATHEx.CFG-file.
; They also relay on the macro "internalToolGeometry" that can also be found
; in yadro.cfg
;
; ---------------------------------------
Macro:#- deactivate tool geometry
  TGX = 0;
  TGZ = 0;
  TTR = 0;
  TGW = 0;

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
                      "X-offset:", TGX,
                      "Z-offset:", TGZ,
                    "Tip radius:", TTZ,
                    "Tool width:", TGW)) {
      // save tools in base unit (mm)

      TGZ = TGZ / UnitScaling;
      TTR = TTR / UnitScaling;
      TGW = TGW / UnitScaling;
      TGX = TGX / UnitScaling;

      perswrite(TGZ);
      perswrite(TTR);
      perswrite(TGW);
      perswrite(TGX);

      // undo scaling
      TGX = TGX * UnitScaling;
      TGZ = TGZ * UnitScaling;
      TTR = TTR * UnitScaling;
      TGW = TGW * UnitScaling;
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
  TGX = TGX * UnitScaling;
  TGZ = TGZ * UnitScaling;
  TTR = TTR * UnitScaling;
  TGW = TGW * UnitScaling;

  execmacro("internalToolGeometry");
:EndM
; ----------------------------------------
; *******************************************************************************
; *** If you want to add more tools, copy a macro and pay attention that the  ***
; *** persread (4*), ShowTool and the macro-name match (6 places to           ***
; *** make adjustments)                                                       ***
; ***                                                                         ***
; ***                                                                         ***
; *******************************************************************************
Macro:#00
  persread(TGX, "ToolGeometry00TGX");
  persread(TGZ, "ToolGeometry00TGZ");
  persread(TGW, "ToolGeometry00TGW");
  persread(TTR, "ToolGeometry00TTR");

  display("ShowTool", "#00");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#01
  persread(TGX, "ToolGeometry01TGX");
  persread(TGZ, "ToolGeometry01TGZ");
  persread(TGW, "ToolGeometry01TGW");
  persread(TTR, "ToolGeometry01TTR");

  display("ShowTool", "#01");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#02
  persread(TGX, "ToolGeometry02TGX");
  persread(TGZ, "ToolGeometry02TGZ");
  persread(TGW, "ToolGeometry02TGW");
  persread(TTR, "ToolGeometry02TTR");

  display("ShowTool", "#02");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#03
  persread(TGX, "ToolGeometry03TGX");
  persread(TGZ, "ToolGeometry03TGZ");
  persread(TGW, "ToolGeometry03TGW");
  persread(TTR, "ToolGeometry03TTR");

  display("ShowTool", "#03");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#04
  persread(TGX, "ToolGeometry04TGX");
  persread(TGZ, "ToolGeometry04TGZ");
  persread(TGW, "ToolGeometry04TGW");
  persread(TTR, "ToolGeometry04TTR");

  display("ShowTool", "#04");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#05
  persread(TGX, "ToolGeometry05TGX");
  persread(TGZ, "ToolGeometry05TGZ");
  persread(TGW, "ToolGeometry05TGW");
  persread(TTR, "ToolGeometry05TTR");

  display("ShowTool", "#05");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#06
  persread(TGX, "ToolGeometry06TGX");
  persread(TGZ, "ToolGeometry06TGZ");
  persread(TGW, "ToolGeometry06TGW");
  persread(TTR, "ToolGeometry06TTR");

  display("ShowTool", "#06");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#07
  persread(TGX, "ToolGeometry07TGX");
  persread(TGZ, "ToolGeometry07TGZ");
  persread(TGW, "ToolGeometry07TGW");
  persread(TTR, "ToolGeometry07TTR");

  display("ShowTool", "#07");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#08
  persread(TGX, "ToolGeometry08TGX");
  persread(TGZ, "ToolGeometry08TGZ");
  persread(TGW, "ToolGeometry08TGW");
  persread(TTR, "ToolGeometry08TTR");

  display("ShowTool", "#08");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#09
  persread(TGX, "ToolGeometry09TGX");
  persread(TGZ, "ToolGeometry09TGZ");
  persread(TGW, "ToolGeometry09TGW");
  persread(TTR, "ToolGeometry09TTR");

  display("ShowTool", "#09");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------
Macro:#10
  persread(TGX, "ToolGeometry10TGX");
  persread(TGZ, "ToolGeometry10TGZ");
  persread(TGW, "ToolGeometry10TGW");
  persread(TTR, "ToolGeometry10TTR");

  display("ShowTool", "#10");
  execmacro("internalTGActivate");
:EndM
; ----------------------------------------

