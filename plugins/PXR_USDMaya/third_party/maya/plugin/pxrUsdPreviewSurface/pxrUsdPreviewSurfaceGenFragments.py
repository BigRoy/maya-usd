#!/pxrpythonsubst
#
# Copyright 2018 Pixar
#
# Licensed under the Apache License, Version 2.0 (the "Apache License")
# with the following modification; you may not use this file except in
# compliance with the Apache License and the following modification to it:
# Section 6. Trademarks. is deleted and replaced with:
#
# 6. Trademarks. This License does not grant permission to use the trade
#    names, trademarks, service marks, or product names of the Licensor
#    and its affiliates, except as required to comply with Section 4(c) of
#    the License and to reproduce the content of the NOTICE file.
#
# You may obtain a copy of the Apache License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the Apache License with the above modification is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the Apache License for the specific
# language governing permissions and limitations under the Apache License.
#
"""
This script generates Maya fragment XML files by extracting sections of the
UsdPreviewSurface shader code from its glslfx file.
"""

import argparse
import difflib
import jinja2
import os


class GlslfxParser(object):
    """
    Reads a glslfx file and allows extracting glsl shader code by section ID.

    Since building the imaging components of USD can be disabled, we cannot use
    any of the facilities in Glf for inspecting the glslfx. Instead, this
    simple parser just breaks down the given file into a mapping of section ID
    to the glsl shader code in that section. All other parts of the file are
    ignored, and no validation is performed.
    """

    COMMENT_DELIMITER = '---'
    SECTION_DELIMITER = '--'
    GLSL_SECTION_TYPE = 'glsl'

    def __init__(self, glslfxFilePath):
        self._glslfxFilePath = glslfxFilePath
        self._Parse()

    def _Parse(self):
        glslfxFile = open(self._glslfxFilePath, 'r')

        currentSectionId = None
        lineNumber = 0

        self._sectionMap = {}

        for line in glslfxFile:
            line = line.rstrip()

            lineNumber += 1

            if line.startswith(GlslfxParser.COMMENT_DELIMITER):
                # Ignore glslfx comments.
                continue

            if line.startswith(GlslfxParser.SECTION_DELIMITER):
                # This is the start of a new section.
                currentSectionId = None

                parts = line.split()
                if len(parts) < 2:
                    raise RuntimeError(
                        'Syntax Error on line %d of %s. section delimiter '
                        'must be followed by a valid token' % (lineNumber,
                            self._glslfxFilePath))

                sectionType = parts[1]
                if sectionType != GlslfxParser.GLSL_SECTION_TYPE:
                    # We ignore all non-glsl sections.
                    continue

                if len(parts) < 3:
                    raise RuntimeError(
                        'Syntax Error on line %d of %s. "glsl" tag must'
                        'be followed by a valid identifier' % (lineNumber,
                            self._glslfxFilePath))

                currentSectionId = parts[2]

                # Leave a breadcrumb to help identify where this code came from.
                self._sectionMap[currentSectionId] = [
                    '// line %d of "%s"' % (lineNumber, self._glslfxFilePath)]
            elif currentSectionId:
                self._sectionMap[currentSectionId].append(line)

        glslfxFile.close()

        for sectionId in self._sectionMap:
            sectionCode = '\n'.join(line for line in self._sectionMap[sectionId])
            self._sectionMap[sectionId] = sectionCode

    def GetSectionCode(self, sectionId):
        return self._sectionMap[sectionId]


def _GenerateFragmentXML(xmlTemplate, outputXmlFilePath, shaderCode=None,
        shaderCodeFilePath=None, verbose=True):
    # Use shaderCode directly if it is provided, otherwise fallback on the file
    # containing the shader code.
    if not shaderCode:
        if not shaderCodeFilePath:
            raise RuntimeError(
                'One of shaderCode or shaderCodeFilePath must be provided')

        if not os.path.exists(shaderCodeFilePath):
            raise RuntimeError(
                'shaderCode not provided and "%s" does not exist' %
                shaderCodeFilePath)

        shaderCodeFile = open(shaderCodeFilePath, 'r')
        shaderCode = shaderCodeFile.read()
        shaderCodeFile.close()

    autoGenWarningComment = (
        '<!-- WARNING: Do not edit this file. It was automatically generated '
        'by pxrUsdPreviewSurfaceGenFragments. -->')
    shaderCodeCdata = '<![CDATA[\n%s\n]]>' % shaderCode

    xmlContent = xmlTemplate.render(autoGenWarning=autoGenWarningComment,
        shaderCode=shaderCodeCdata)

    # If file currently exists and content is unchanged, do nothing.
    existingContent = '\n'
    if os.path.exists(outputXmlFilePath):
        with open(outputXmlFilePath, 'r') as xmlFile:
            existingContent = xmlFile.read()
            if existingContent == xmlContent:
                if verbose:
                    print '\tunchanged %s' % outputXmlFilePath
                return

    # Otherwise attempt to write to file.
    try:
        with open(outputXmlFilePath, 'w') as xmlFile:
            xmlFile.write(xmlContent)
            if verbose:
                print '\t    wrote %s' % outputXmlFilePath
    except IOError as ioe:
        print '\t', ioe
        print 'Diff:'
        print '\n'.join(difflib.unified_diff(existingContent.split('\n'),
            xmlContent.split('\n')))


def _GenerateLightingContributionsShaderCode(glslfxParser):
    codeHeader = \
"""
// Check whether we're in a GLSL context. If so, we don't expect "floatN"
// types to exist, so we use "vecN" types instead.
#if defined(__VERSION__) && (__VERSION__ >= 110)
#define float3 vec3
#endif

"""

    glslCode = glslfxParser.GetSectionCode('Preview.LightStructures')
    glslCode = glslCode.replace('vec3', 'float3')

    shaderCode = "%s%s" % (codeHeader, glslCode)

    return shaderCode


def _GenerateLightingShaderCode(glslfxParser):
    codeHeader = \
"""
// Check whether we're in a GLSL context. If so, we don't expect "floatN"
// types to exist, so we use "vecN" types instead.
#if defined(__VERSION__) && (__VERSION__ >= 110)
#define float3 vec3
#endif

"""

    glslCode = glslfxParser.GetSectionCode('Preview.Lighting')
    glslCode = glslCode.replace('vec3', 'float3')

    # Replace the name of the main entry point.
    glslCode = glslCode.replace('evaluateLight(', 'usdPreviewSurfaceLighting(')

    shaderCode = "%s%s" % (codeHeader, glslCode)

    return shaderCode


def _ValidateXML(xmlFilePath, xmlSchemaFilePath):
    if not xmlSchemaFilePath:
        return

    try:
        from lxml import etree
    except ImportError:
        print "Could not import lxml.etree. NOT validating XML against schema"
        return

    xmlTree = etree.parse(xmlFilePath)

    xsdTree = etree.parse(xmlSchemaFilePath)
    xmlSchema = etree.XMLSchema(etree=xsdTree)

    try:
        xmlSchema.assertValid(xmlTree)
    except etree.DocumentInvalid:
        print "ERROR: XML file '%s' failed validation for schema '%s'" % (
            xmlFilePath, xmlSchemaFilePath)
        raise


def _RemoveBreadcrumbComments(codeLines):
    cleaned = []
    for line in codeLines:
        if line.strip().startswith('// line '):
            continue
        cleaned.append(line)
    return cleaned

# Check that each file in dstDir matches the corresponding file in srcDir.
def _ValidateGeneratedFiles(srcDir, dstDir):
    import difflib
    missing = []
    diffs = []
    for dstFile in [os.path.join(dstDir, f) for f in os.listdir(dstDir)
                    if os.path.isfile(os.path.join(dstDir, f))]:
        srcFile = os.path.join(srcDir, os.path.basename(dstFile))
        if not os.path.isfile(srcFile):
            missing.append(srcFile)
            continue
        dstContent = open(dstFile).read().split('\n')
        dstContent = _RemoveBreadcrumbComments(dstContent)
        srcContent = open(srcFile).read().split('\n')
        srcContent = _RemoveBreadcrumbComments(srcContent)
        if dstContent != srcContent:
            diff = '\n'.join(difflib.unified_diff(
                srcContent,
                dstContent,
                'Source ' + os.path.basename(srcFile),
                'Generated ' + os.path.basename(dstFile)))
            diffs.append(diff)
            continue

    if missing or diffs:
        msg = []
        if missing:
            msg.append('*** Missing Generated Files: ' + ', '.join(missing))
        if diffs:
            msg.append('*** Differing Generated Files:\n' + '\n'.join(diffs))
        raise RuntimeError('\n' + '\n'.join(msg))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Generate Maya fragment XML files for pxrUsdPreviewSurface')

    parser.add_argument('glslfxFilePath', type=str,
        help='glslfx file from which to extract UsdPreviewSurface shader code')
    parser.add_argument('--srcDir', type=str,
        help=('Directory where XML templates and shader code files can be '
            'found (default is cwd)'),
        default=os.getcwd())
    parser.add_argument('--dstDir', type=str,
        help=('Directory where fragment XML files should be written (default '
            'is cwd)'),
        default=os.getcwd())
    parser.add_argument('--schema', type=str,
        help='XSD file to use for validating generated XML')
    parser.add_argument('--validate', action='store_true')

    args = parser.parse_args()

    if args.validate:
        # Make a temporary directory for results.
        import tempfile
        args.dstDir = tempfile.mkdtemp()

    glslfxParser = GlslfxParser(args.glslfxFilePath)

    # The shader code for the lightingContributions and
    # usdPreviewSurfaceLighting fragments is extracted from the glslfx file.
    # All of the other fragments are expected to have local shader code files.
    lightingContributionsShaderCode = _GenerateLightingContributionsShaderCode(
        glslfxParser)
    lightingShaderCode = _GenerateLightingShaderCode(glslfxParser)

    jinjaEnv = jinja2.Environment(loader=jinja2.FileSystemLoader(args.srcDir))

    shaderCodeMap = {
        'lightingContributions': lightingContributionsShaderCode,
        'usdPreviewSurfaceLighting': lightingShaderCode
    }

    genFragNames = [
        'float4ToFloatX',
        'float4ToFloatY',
        'float4ToFloatZ',
        'float4ToFloatW',
        'lightingContributions',
        'usdPreviewSurfaceCombiner',
        'usdPreviewSurfaceLighting'
    ]

    for fragName in genFragNames:
        xmlTemplateFileName = '%s.jinja2.xml' % fragName
        shaderCode = shaderCodeMap.get(fragName)
        shaderCodeFilePath = '%s/%s_shaderCode.glsl' % (args.srcDir, fragName)
        outputXmlFilePath = '%s/%s.xml' % (args.dstDir, fragName)

        xmlTemplate = jinjaEnv.get_template(xmlTemplateFileName)

        _GenerateFragmentXML(xmlTemplate, outputXmlFilePath,
            shaderCode=shaderCode, shaderCodeFilePath=shaderCodeFilePath,
            verbose=not args.validate)

        _ValidateXML(outputXmlFilePath, args.schema)

    if args.validate:
        _ValidateGeneratedFiles(args.srcDir, args.dstDir)