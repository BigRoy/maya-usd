//Maya ASCII 2016 scene
//Name: EmptyScene.ma
//Last modified: Thu, Apr 19, 2018 07:22:43 PM
//Codeset: UTF-8
requires maya "2016";
currentUnit -l centimeter -a degree -t film;
fileInfo "application" "maya";
fileInfo "product" "Maya 2016";
fileInfo "version" "2016";
fileInfo "cutIdentifier" "201610262200-1005964";
fileInfo "osv" "Linux 3.10.0-693.2.2.el7.x86_64 #1 SMP Sat Sep 9 03:55:24 EDT 2017 x86_64";
createNode transform -s -n "persp";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED400000224";
	setAttr ".v" no;
	setAttr ".t" -type "double3" 877 -878 658 ;
	setAttr ".r" -type "double3" 62.066155704526331 6.3611093629270351e-15 44.967352835719474 ;
createNode camera -s -n "perspShape" -p "persp";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED400000225";
	setAttr -k off ".v" no;
	setAttr ".fl" 34.999999999999993;
	setAttr ".coi" 1404.6269967503829;
	setAttr ".imn" -type "string" "persp";
	setAttr ".den" -type "string" "persp_depth";
	setAttr ".man" -type "string" "persp_mask";
	setAttr ".hc" -type "string" "viewSet -p %camera";
createNode transform -s -n "top";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED400000226";
	setAttr ".v" no;
	setAttr ".t" -type "double3" 0 0 365.86 ;
createNode camera -s -n "topShape" -p "top";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED400000227";
	setAttr -k off ".v" no;
	setAttr ".rnd" no;
	setAttr ".coi" 365.86;
	setAttr ".ow" 30;
	setAttr ".imn" -type "string" "top";
	setAttr ".den" -type "string" "top_depth";
	setAttr ".man" -type "string" "top_mask";
	setAttr ".hc" -type "string" "viewSet -t %camera";
	setAttr ".o" yes;
createNode transform -s -n "front";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED400000228";
	setAttr ".v" no;
	setAttr ".t" -type "double3" 0 -365.86 0 ;
	setAttr ".r" -type "double3" 89.999999999999986 0 0 ;
createNode camera -s -n "frontShape" -p "front";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED400000229";
	setAttr -k off ".v" no;
	setAttr ".rnd" no;
	setAttr ".coi" 365.86;
	setAttr ".ow" 30;
	setAttr ".imn" -type "string" "front";
	setAttr ".den" -type "string" "front_depth";
	setAttr ".man" -type "string" "front_mask";
	setAttr ".hc" -type "string" "viewSet -f %camera";
	setAttr ".o" yes;
createNode transform -s -n "side";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED40000022A";
	setAttr ".v" no;
	setAttr ".t" -type "double3" 365.86 0 0 ;
	setAttr ".r" -type "double3" 90 1.2722218725854067e-14 89.999999999999986 ;
createNode camera -s -n "sideShape" -p "side";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED40000022B";
	setAttr -k off ".v" no;
	setAttr ".rnd" no;
	setAttr ".coi" 365.86;
	setAttr ".ow" 30;
	setAttr ".imn" -type "string" "side";
	setAttr ".den" -type "string" "side_depth";
	setAttr ".man" -type "string" "side_mask";
	setAttr ".hc" -type "string" "viewSet -s %camera";
	setAttr ".o" yes;
createNode lightLinker -s -n "lightLinker1";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED40000022C";
	setAttr -s 2 ".lnk";
	setAttr -s 2 ".slnk";
createNode displayLayerManager -n "layerManager";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED500000241";
createNode displayLayer -n "defaultLayer";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED500000242";
createNode renderLayerManager -n "renderLayerManager";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED500000243";
createNode renderLayer -n "defaultRenderLayer";
	rename -uid "F7F2A8C0-0000-08D5-5AD9-4ED500000244";
	setAttr ".g" yes;
select -ne :time1;
	setAttr ".o" 1;
	setAttr ".unw" 1;
select -ne :hardwareRenderingGlobals;
	setAttr ".otfna" -type "stringArray" 22 "NURBS Curves" "NURBS Surfaces" "Polygons" "Subdiv Surface" "Particles" "Particle Instance" "Fluids" "Strokes" "Image Planes" "UI" "Lights" "Cameras" "Locators" "Joints" "IK Handles" "Deformers" "Motion Trails" "Components" "Hair Systems" "Follicles" "Misc. UI" "Ornaments"  ;
	setAttr ".otfva" -type "Int32Array" 22 0 1 1 1 1 1
		 1 1 1 0 0 0 0 0 0 0 0 0
		 0 0 0 0 ;
	setAttr ".fprt" yes;
select -ne :renderPartition;
	setAttr -s 2 ".st";
select -ne :renderGlobalsList1;
select -ne :defaultShaderList1;
	setAttr -s 4 ".s";
select -ne :postProcessList1;
	setAttr -s 2 ".p";
select -ne :defaultRenderingList1;
select -ne :initialShadingGroup;
	setAttr ".ro" yes;
select -ne :initialParticleSE;
	setAttr ".ro" yes;
select -ne :defaultResolution;
	setAttr ".pa" 1;
select -ne :hardwareRenderGlobals;
	setAttr ".ctrs" 256;
	setAttr ".btrs" 512;
relationship "link" ":lightLinker1" ":initialShadingGroup.message" ":defaultLightSet.message";
relationship "link" ":lightLinker1" ":initialParticleSE.message" ":defaultLightSet.message";
relationship "shadowLink" ":lightLinker1" ":initialShadingGroup.message" ":defaultLightSet.message";
relationship "shadowLink" ":lightLinker1" ":initialParticleSE.message" ":defaultLightSet.message";
connectAttr "layerManager.dli[0]" "defaultLayer.id";
connectAttr "renderLayerManager.rlmi[0]" "defaultRenderLayer.rlid";
connectAttr "defaultRenderLayer.msg" ":defaultRenderingList1.r" -na;
// End of EmptyScene.ma