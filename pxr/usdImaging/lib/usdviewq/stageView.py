#
# Copyright 2016 Pixar
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
'''
Module that provides the StageView class.
'''

from math import tan, atan, floor, ceil, radians as rad
import os
from time import time

from qt import QtCore, QtGui, QtWidgets, QtOpenGL

from pxr import Tf
from pxr import Gf
from pxr import Glf
from pxr import Sdf, Usd, UsdGeom
from pxr import UsdImagingGL
from pxr import CameraUtil

from common import (RENDER_MODE_WIREFRAME, RENDER_MODE_WIREFRAME_ON_SURFACE,
                    RENDER_MODE_SMOOTH_SHADED, RENDER_MODE_FLAT_SHADED,
                    RENDER_MODE_POINTS, RENDER_MODE_GEOM_ONLY,
                    RENDER_MODE_GEOM_FLAT, RENDER_MODE_GEOM_SMOOTH,
                    RENDER_MODE_HIDDEN_SURFACE_WIREFRAME)

DEBUG_CLIPPING = "USDVIEWQ_DEBUG_CLIPPING"

# A viewport rectangle to be used for GL must be integer values.
# In order to loose the least amount of precision the viewport
# is centered and adjusted to initially contain entirely the
# given viewport.
# If it turns out that doing so gives more than a pixel width
# or height of error the viewport is instead inset.
# This does mean that the returned viewport may have a slightly
# different aspect ratio to the given viewport.
def ViewportMakeCenteredIntegral(viewport):

    # The values are initially integral and containing the
    # the given rect
    left = int(floor(viewport[0]))
    bottom = int(floor(viewport[1]))
    right = int(ceil(viewport[0] + viewport[2]))
    top = int(ceil(viewport[1] + viewport[3]))

    width = right - left
    height = top - bottom

    # Compare the integral height to the original height
    # and do a centered 1 pixel adjustment if more than
    # a pixel off.
    if (height - viewport[3]) > 1.0:
        bottom += 1
        height -= 2
    # Compare the integral width to the original width
    # and do a centered 1 pixel adjustment if more than
    # a pixel off.
    if (width - viewport[2]) > 1.0:
        left += 1
        width -= 2
    return (left, bottom, width, height)

# FreeCamera inherits from QObject only so that it can send signals...
# which is really a pretty nice, easy to use notification system.
class FreeCamera(QtCore.QObject):

    # Allows FreeCamera owner to act when the camera's relationship to
    # its viewed content changes.  For instance, to compute the value
    # to supply for setClosestVisibleDistFromPoint()
    signalFrustumChanged = QtCore.Signal()

    defaultNear = 1
    defaultFar = 1000000
    # Experimentally on Nvidia M6000, if Far/Near is greater than this,
    # then geometry in the back half of the volume will disappear
    maxSafeZResolution = 1e6
    # Experimentally on Nvidia M6000, if Far/Near is greater than this,
    # then we will often see Z-fighting artifacts even for geometry that
    # is close to camera
    maxGoodZResolution = 5e4

    def __init__(self, isZUp):
        """FreeCamera can be either a Z up or Y up camera, based on 'zUp'"""
        super(FreeCamera, self).__init__()

        self._camera = Gf.Camera()
        self._camera.SetPerspectiveFromAspectRatioAndFieldOfView(
            1.0, 60, Gf.Camera.FOVVertical)
        self._camera.clippingRange = Gf.Range1f(FreeCamera.defaultNear,
                                                FreeCamera.defaultFar)
        self._overrideNear = None
        self._overrideFar = None
        self._isZUp = isZUp

        self._cameraTransformDirty = True
        self._rotTheta = 0
        self._rotPhi = 0
        self._rotPsi = 0
        self._center = Gf.Vec3d(0,0,0)
        self._dist = 100
        self._camera.focusDistance = self._dist
        self._closestVisibleDist = None
        self._lastFramedDist = None
        self._lastFramedClosestDist = None
        self._selSize = 10

        if isZUp:
            # This is also Gf.Camera.Y_UP_TO_Z_UP_MATRIX
            self._YZUpMatrix = Gf.Matrix4d().SetRotate(
                Gf.Rotation(Gf.Vec3d.XAxis(), -90))
            self._YZUpInvMatrix = self._YZUpMatrix.GetInverse()
        else:
            self._YZUpMatrix = Gf.Matrix4d(1.0)
            self._YZUpInvMatrix = Gf.Matrix4d(1.0)

    # Why a clone() method vs copy.deepcopy()ing the FreeCamera ?
    # 1) Several of the Gf classes are not python-picklable (requirement of
    #    deepcopy), nor is GfCamera.  Adding that infrastructure for this
    #    single client seems weighty.
    # 2) We could make FreeCamera itself be picklable... that solution would
    #    require twice as much code as clone().  If we wind up extracting
    #    FreeCamera to be a more general building block, it may be worth it,
    #    and clone() would transition to __getstate__().
    def clone(self):
        clone = FreeCamera(self._isZUp)
        clone._camera = Gf.Camera(self._camera)
        # skipping stereo attrs for now

        clone._rotTheta = self._rotTheta
        clone._rotPhi = self._rotPhi
        clone._rotPsi = self._rotPsi
        clone._center = Gf.Vec3d(self._center)
        clone._dist = self._dist
        clone._closestVisibleDist = self._closestVisibleDist
        clone._lastFramedClosestDist = self._lastFramedClosestDist
        clone._lastFramedDist = self._lastFramedDist
        clone._selSize = self._selSize
        clone._overrideNear = self._overrideNear
        clone._overrideFar = self._overrideFar
        clone._YZUpMatrix = Gf.Matrix4d(self._YZUpMatrix)
        clone._YZUpInvMatrix = Gf.Matrix4d(self._YZUpInvMatrix)

        return clone


    def _updateCameraTransform(self):
        """
        Updates the camera's transform matrix, that is, the matrix that brings
        the camera to the origin, with the camera view pointing down:
           +Y if this is a Zup camera, or
           -Z if this is a Yup camera .
        """
        if not self._cameraTransformDirty:
            return

        def RotMatrix(vec, angle):
            return Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(vec, angle))

        # self._YZUpInvMatrix influences the behavior about how the
        # FreeCamera will tumble. It is the identity or a rotation about the
        # x-Axis.
        self._camera.transform = (
            Gf.Matrix4d().SetTranslate(Gf.Vec3d.ZAxis() * self.dist) *
            RotMatrix(Gf.Vec3d.ZAxis(), -self._rotPsi) *
            RotMatrix(Gf.Vec3d.XAxis(), -self._rotPhi) *
            RotMatrix(Gf.Vec3d.YAxis(), -self._rotTheta) *
            self._YZUpInvMatrix *
            Gf.Matrix4d().SetTranslate(self.center))
        self._camera.focusDistance = self.dist

        self._cameraTransformDirty = False

    def _rangeOfBoxAlongRay(self, camRay, bbox, debugClipping=False):
        maxDist = -float('inf')
        minDist = float('inf')
        boxRange = bbox.GetRange()
        boxXform = bbox.GetMatrix()
        for i in range (8):
            # for each corner of the bounding box, transform to world
            # space and project
            point = boxXform.Transform(boxRange.GetCorner(i))
            pointDist = camRay.FindClosestPoint(point)[1]

            # find the projection of that point of the camera ray
            # and find the farthest and closest point.
            if pointDist > maxDist:
                maxDist = pointDist
            if pointDist < minDist:
                minDist = pointDist

        if debugClipping:
            print "Projected bounds near/far: %f, %f" % (minDist, maxDist)

        # if part of the bbox is behind the ray origin (i.e. camera),
        # we clamp minDist to be positive.  Otherwise, reduce minDist by a bit
        # so that geometry at exactly the edge of the bounds won't be clipped -
        # do the same for maxDist, also!
        if minDist < FreeCamera.defaultNear:
            minDist = FreeCamera.defaultNear
        else:
            minDist *= 0.99
        maxDist *= 1.01

        if debugClipping:
            print "Contracted bounds near/far: %f, %f" % (minDist, maxDist)

        return minDist, maxDist

    def setClippingPlanes(self, stageBBox):
        '''Computes and sets automatic clipping plane distances using the
           camera's position and orientation, the bouding box
           surrounding the stage, and the distance to the closest rendered
           object in the central view of the camera (closestVisibleDist).

           If either of the "override" clipping attributes are not None,
           we use those instead'''

        debugClipping = Tf.Debug.IsDebugSymbolNameEnabled(DEBUG_CLIPPING)

        # If the scene bounding box is empty, or we are fully on manual
        # override, then just initialize to defaults.
        if stageBBox.GetRange().IsEmpty() or \
               (self._overrideNear and self._overrideFar) :
            computedNear, computedFar = FreeCamera.defaultNear, FreeCamera.defaultFar
        else:
            # The problem: We want to include in the camera frustum all the
            # geometry the viewer should be able to see, i.e. everything within
            # the inifinite frustum starting at distance epsilon from the
            # camera itself.  However, the further the imageable geometry is
            # from the near-clipping plane, the less depth precision we will
            # have to resolve nearly colinear/incident polygons (which we get
            # especially with any doubleSided geometry).  We can run into such
            # situations astonishingly easily with large sets when we are
            # focussing in on just a part of a set that spans 10^5 units or
            # more.
            #
            # Our solution: Begin by projecting the endpoints of the imageable
            # world's bounds onto the ray piercing the center of the camera
            # frustum, and take the near/far clipping distances from its
            # extent, clamping at a positive value for near.  To address the
            # z-buffer precision issue, we rely on someone having told us how
            # close the closest imageable geometry actually is to the camera,
            # by having called setClosestVisibleDistFromPoint(). This gives us
            # the most liberal near distance we can use and not clip the
            # geometry we are looking at.  We actually choose some fraction of
            # that distance instead, because we do not expect the someone to
            # recompute the closest point with every camera manipulation, as
            # it can be expensive (we do emit signalFrustumChanged to notify
            # them, however).  We only use this if the current range of the
            # bbox-based frustum will have precision issues.
            frustum = self._camera.frustum
            camPos = frustum.position

            camRay = Gf.Ray(camPos, frustum.ComputeViewDirection())
            computedNear, computedFar = self._rangeOfBoxAlongRay(camRay,
                                                                 stageBBox,
                                                                 debugClipping)

            precisionNear = computedFar / FreeCamera.maxGoodZResolution

            if debugClipping:
                print "Proposed near for precision: {}, closestDist: {}"\
                    .format(precisionNear, self._closestVisibleDist)
            if self._closestVisibleDist:
                # Because of our concern about orbit/truck causing
                # clipping, make sure we don't go closer than half the
                # distance to the closest visible point
                halfClose = self._closestVisibleDist / 2.0

                if self._closestVisibleDist < self._lastFramedClosestDist:
                    # This can happen if we have zoomed in closer since
                    # the last time setClosestVisibleDistFromPoint() was called.
                    # Clamp to precisionNear, which gives a balance between
                    # clipping as we zoom in, vs bad z-fighting as we zoom in.
                    # See adjustDist() for comment about better solution.
                    halfClose = max(precisionNear, halfClose, computedNear)
                    if debugClipping:
                        print "ADJUSTING: Accounting for zoom-in"

                if halfClose < computedNear:
                    # If there's stuff very very close to the camera, it
                    # may have been clipped by computedNear.  Get it back!
                    computedNear = halfClose
                    if debugClipping:
                        print "ADJUSTING: closestDist was closer than bboxNear"
                elif precisionNear > computedNear:
                    computedNear = min((precisionNear + halfClose) / 2.0,
                                       halfClose)
                    if debugClipping:
                        print "ADJUSTING: gaining precision by pushing out"

        near = self._overrideNear or computedNear
        far  = self._overrideFar  or computedFar
        # Make sure far is greater than near
        far = max(near+1, far)

        if debugClipping:
            print "***Final Near/Far: {}, {}".format(near, far)

        self._camera.clippingRange = Gf.Range1f(near, far)

    def computeGfCamera(self, stageBBox):
        """Makes sure the FreeCamera's computed parameters are up-to-date, and
        returns the GfCamera object."""
        self._updateCameraTransform()
        self.setClippingPlanes(stageBBox)
        return self._camera

    def frameSelection(self, selBBox, frameFit):
        # needs to be recomputed
        self._closestVisibleDist = None

        self.center = selBBox.ComputeCentroid()
        selRange = selBBox.ComputeAlignedRange()
        self._selSize = max(*selRange.GetSize())
        if self.orthographic:
            self.fov = self._selSize * Gf.Camera.APERTURE_UNIT * frameFit
            self.dist = self._selSize
        else:
            halfFov = self.fov*0.5 or 0.5 # don't divide by zero
            self.dist = ((self._selSize * frameFit * 0.5)
                         / atan(rad(halfFov)))

    def setClosestVisibleDistFromPoint(self, point):
        frustum = self._camera.frustum
        camPos = frustum.position
        camRay = Gf.Ray(camPos, frustum.ComputeViewDirection())
        self._closestVisibleDist = camRay.FindClosestPoint(point)[1]
        self._lastFramedDist = self.dist
        self._lastFramedClosestDist = self._closestVisibleDist

        if Tf.Debug.IsDebugSymbolNameEnabled(DEBUG_CLIPPING):
            print "Resetting closest distance to {}; CameraPos: {}, closestPoint: {}".format(self._closestVisibleDist, camPos, point)

    def adjustDist(self, scaleFactor):
        # When dist gets very small, you can get stuck and not be able to
        # zoom back out, if you just keep multiplying.  Switch to addition
        # in that case, choosing an incr that works for the scale of the
        # framed geometry.
        if scaleFactor > 1 and self.dist < 2:
            selBasedIncr = self._selSize / 25.0
            scaleFactor -= 1.0
            self.dist += min(selBasedIncr, scaleFactor)
        else:
            self.dist *= scaleFactor

        # Make use of our knowledge that we are changing distance to camera
        # to also adjust _closestVisibleDist to keep it useful.  Make sure
        # not to recede farther than the last *computed* closeDist, since that
        # will generally cause unwanted clipping of close objects.
        # XXX:  This heuristic does a good job of preventing undesirable
        # clipping as we zoom in and out, but sacrifices the z-buffer
        # precision we worked hard to get.  If Hd/UsdImaging could cheaply
        # provide us with the closest-point from the last-rendered image,
        # we could use it safely here to update _closestVisibleDist much
        # more accurately than this calculation.
        if self._closestVisibleDist:
            if self.dist > self._lastFramedDist:
                self._closestVisibleDist = self._lastFramedClosestDist
            else:
                self._closestVisibleDist = \
                    self._lastFramedClosestDist - \
                    self._lastFramedDist + \
                    self.dist

    def Truck(self, offX, offY, height):
        self._updateCameraTransform()
        frustum = self._camera.frustum
        cam_up = frustum.ComputeUpVector()
        cam_right = Gf.Cross(frustum.ComputeViewDirection(), cam_up)

        # Figure out distance in world space of a point 'dist' into the
        # screen from center to top of frame
        offRatio = frustum.window.GetSize()[1] * self._dist / height

        self.center += - offRatio * offX * cam_right
        self.center +=   offRatio * offY * cam_up

        self._cameraTransformDirty = True
        self.signalFrustumChanged.emit()

    @staticmethod
    def FromGfCamera(cam, isZUp):
        # Get the data from the camera and its frustum
        cam_transform = cam.transform
        dist = cam.focusDistance
        frustum = cam.frustum
        cam_pos = frustum.position
        cam_axis = frustum.ComputeViewDirection()

        # Create a new FreeCamera setting the camera to be the given camera
        self = FreeCamera(isZUp)
        self._camera = cam

        # Compute translational parts
        self._dist = dist
        self._selSize = dist / 10.0
        self._center = cam_pos + dist * cam_axis

        # self._YZUpMatrix influences the behavior about how the
        # FreeCamera will tumble. It is the identity or a rotation about the
        # x-Axis.

        # Compute rotational part
        transform = cam_transform * self._YZUpMatrix
        transform.Orthonormalize()
        rotation = transform.ExtractRotation()

        # Decompose and set angles
        self._rotTheta, self._rotPhi, self._rotPsi =-rotation.Decompose(
            Gf.Vec3d.YAxis(), Gf.Vec3d.XAxis(), Gf.Vec3d.ZAxis())

        self._cameraTransformDirty = True

        return self

    @property
    def rotTheta(self):
        return self._rotTheta

    @rotTheta.setter
    def rotTheta(self, value):
        self._rotTheta = value
        self._cameraTransformDirty = True
        self.signalFrustumChanged.emit()

    @property
    def rotPhi(self):
        return self._rotPhi

    @rotPhi.setter
    def rotPhi(self, value):
        self._rotPhi = value
        self._cameraTransformDirty = True
        self.signalFrustumChanged.emit()

    @property
    def center(self):
        return self._center

    @center.setter
    def center(self, value):
        self._center = value
        self._cameraTransformDirty = True
        self.signalFrustumChanged.emit()

    @property
    def dist(self):
        return self._dist

    @dist.setter
    def dist(self, value):
        self._dist = value
        self._cameraTransformDirty = True
        self.signalFrustumChanged.emit()

    @property
    def orthographic(self):
        return self._camera.projection == Gf.Camera.Orthographic

    @orthographic.setter
    def orthographic(self, orthographic):
        if orthographic:
            self._camera.projection = Gf.Camera.Orthographic
        else:
            self._camera.projection = Gf.Camera.Perspective
        self.signalFrustumChanged.emit()

    @property
    def fov(self):
        if self._camera.projection == Gf.Camera.Perspective:
            return self._camera.GetFieldOfView(Gf.Camera.FOVVertical)
        else:
            return (self._camera.verticalAperture * Gf.Camera.APERTURE_UNIT)

    @fov.setter
    def fov(self, value):
        if self._camera.projection == Gf.Camera.Perspective:
            self._camera.SetPerspectiveFromAspectRatioAndFieldOfView(
                self._camera.aspectRatio, value, Gf.Camera.FOVVertical)
        else:
            self._camera.SetOrthographicFromAspectRatioAndSize(
                self._camera.aspectRatio, value, Gf.Camera.FOVVertical)
        self.signalFrustumChanged.emit()

    @property
    def near(self):
        return self._camera.clippingRange.min


    @property
    def far(self):
        return self._camera.clippingRange.max

    # no setters for near and far - one must set overrideNear/Far instead
    @property
    def overrideNear(self):
        return self._overrideNear

    @overrideNear.setter
    def overrideNear(self, value):
        """To remove the override, set to None"""
        self._overrideNear = value

    @property
    def overrideFar(self):
        return self._overrideFar

    @overrideFar.setter
    def overrideFar(self, value):
        """To remove the override, set to None"""
        self._overrideFar = value

class GLSLProgram():
    def __init__(self, VS3, FS3, VS2, FS2, uniformDict):
        from OpenGL import GL
        self._glMajorVersion = int(GL.glGetString(GL.GL_VERSION)[0])

        self.program   = GL.glCreateProgram()
        vertexShader   = GL.glCreateShader(GL.GL_VERTEX_SHADER)
        fragmentShader = GL.glCreateShader(GL.GL_FRAGMENT_SHADER)

        if (self._glMajorVersion >= 3):
            vsSource = VS3
            fsSource = FS3
        else:
            vsSource = VS2
            fsSource = FS2

        GL.glShaderSource(vertexShader, vsSource)
        GL.glCompileShader(vertexShader)
        GL.glShaderSource(fragmentShader, fsSource)
        GL.glCompileShader(fragmentShader)
        GL.glAttachShader(self.program, vertexShader)
        GL.glAttachShader(self.program, fragmentShader)
        GL.glLinkProgram(self.program)

        if GL.glGetProgramiv(self.program, GL.GL_LINK_STATUS) == GL.GL_FALSE:
            print GL.glGetShaderInfoLog(vertexShader)
            print GL.glGetShaderInfoLog(fragmentShader)
            print GL.glGetProgramInfoLog(self.program)
            GL.glDeleteShader(vertexShader)
            GL.glDeleteShader(fragmentShader)
            GL.glDeleteProgram(self.program)
            self.program = 0

        GL.glDeleteShader(vertexShader)
        GL.glDeleteShader(fragmentShader)

        self.uniformLocations = {}
        for param in uniformDict:
            self.uniformLocations[param] = GL.glGetUniformLocation(self.program, param)

    def uniform4f(self, param, x, y, z, w):
        from OpenGL import GL
        GL.glUniform4f(self.uniformLocations[param], x, y, z, w)

class Rect():
    def __init__(self):
        self.xywh = [0.0] * 4

    @classmethod
    def fromXYWH(cls, xywh):
        self = cls()
        self.xywh[:] = map(float, xywh[:4])
        return self

    @classmethod
    def fromCorners(cls, c0, c1):
        self = cls()
        self.xywh[0] = float(min(c0[0], c1[0]))
        self.xywh[1] = float(min(c0[1], c1[1]))
        self.xywh[2] = float(max(c0[0], c1[0])) - self.xywh[0]
        self.xywh[3] = float(max(c0[1], c1[1])) - self.xywh[1]
        return self

    def scaledAndBiased(self, sxy, txy):
        ret = self.__class__()
        for c in range(2):
            ret.xywh[c] = sxy[c] * self.xywh[c] + txy[c]
            ret.xywh[c + 2] = sxy[c] * self.xywh[c + 2]
        return ret

    def _splitAlongY(self, y):
        bottom = self.__class__()
        top = self.__class__()
        bottom.xywh[:] = self.xywh
        top.xywh[:] = self.xywh
        top.xywh[1] = y
        bottom.xywh[3] = top.xywh[1] - bottom.xywh[1]
        top.xywh[3] = top.xywh[3] - bottom.xywh[3]
        return bottom, top

    def _splitAlongX(self, x):
        left = self.__class__()
        right = self.__class__()
        left.xywh[:] = self.xywh
        right.xywh[:] = self.xywh
        right.xywh[0] = x
        left.xywh[2] = right.xywh[0] - left.xywh[0]
        right.xywh[2] = right.xywh[2] - left.xywh[2]
        return left, right

    def difference(self, xywh):
        #check x
        if xywh[0] > self.xywh[0]:
            #keep left, check right
            left, right = self._splitAlongX(xywh[0])
            return [left] + right.difference(xywh)
        if (xywh[0] + xywh[2]) < (self.xywh[0] + self.xywh[2]):
            #keep right
            left, right = self._splitAlongX(xywh[0] + xywh[2])
            return [right]
        #check y
        if xywh[1] > self.xywh[1]:
            #keep bottom, check top
            bottom, top = self._splitAlongY(xywh[1])
            return [bottom] + top.difference(xywh)
        if (xywh[1] + xywh[3]) < (self.xywh[1] + self.xywh[3]):
            #keep top
            bottom, top = self._splitAlongY(xywh[1] + xywh[3])
            return [top]
        return []


class OutlineRect(Rect):
    _glslProgram = None
    _vbo = 0
    _vao = 0
    def __init__(self):
        Rect.__init__(self)

    @classmethod
    def compileProgram(self):
        if self._glslProgram:
            return self._glslProgram
        from OpenGL import GL
        import ctypes

        # prep a quad line vbo
        self._vbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        st = [0, 0, 1, 0, 1, 1, 0, 1]
        GL.glBufferData(GL.GL_ARRAY_BUFFER, len(st)*4,
                        (ctypes.c_float*len(st))(*st), GL.GL_STATIC_DRAW)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

        self._glslProgram = GLSLProgram(
            # for OpenGL 3.1 or later
            """#version 140
               uniform vec4 rect;
               in vec2 st;
               void main() {
                 gl_Position = vec4(rect.x + rect.z*st.x,
                                    rect.y + rect.w*st.y, 0, 1); }""",
            """#version 140
               out vec4 fragColor;
               uniform vec4 color;
              void main() { fragColor = color; }""",
            # for OpenGL 2.1 (osx compatibility profile)
            """#version 120
               uniform vec4 rect;
               attribute vec2 st;
               void main() {
                 gl_Position = vec4(rect.x + rect.z*st.x,
                                    rect.y + rect.w*st.y, 0, 1); }""",
            """#version 120
               uniform vec4 color;
               void main() { gl_FragColor = color; }""",
            ["rect", "color"])

        return self._glslProgram

    def glDraw(self, color):
        from OpenGL import GL

        cls = self.__class__

        program = cls.compileProgram()
        if (program.program == 0):
            return

        GL.glUseProgram(program.program)

        if (program._glMajorVersion >= 4):
            GL.glDisable(GL.GL_SAMPLE_ALPHA_TO_COVERAGE)

        # requires PyOpenGL 3.0.2 or later for glGenVertexArrays.
        if (program._glMajorVersion >= 3 and hasattr(GL, 'glGenVertexArrays')):
            if (cls._vao == 0):
                cls._vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(cls._vao)

        # for some reason, we need to bind at least 1 vertex attrib (is OSX)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, cls._vbo)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 0, None)

        program.uniform4f("color", *color)
        program.uniform4f("rect", *self.xywh)
        GL.glDrawArrays(GL.GL_LINE_LOOP, 0, 4)

class FilledRect(Rect):
    _glslProgram = None
    _vbo = 0
    _vao = 0
    def __init__(self):
        Rect.__init__(self)

    @classmethod
    def compileProgram(self):
        if self._glslProgram:
            return self._glslProgram
        from OpenGL import GL
        import ctypes

        # prep a quad line vbo
        self._vbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        st = [0, 0, 1, 0, 0, 1, 1, 1]
        GL.glBufferData(GL.GL_ARRAY_BUFFER, len(st)*4,
                        (ctypes.c_float*len(st))(*st), GL.GL_STATIC_DRAW)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

        self._glslProgram = GLSLProgram(
            # for OpenGL 3.1 or later
            """#version 140
               uniform vec4 rect;
               in vec2 st;
               void main() {
                 gl_Position = vec4(rect.x + rect.z*st.x,
                                    rect.y + rect.w*st.y, 0, 1); }""",
            """#version 140
               out vec4 fragColor;
               uniform vec4 color;
              void main() { fragColor = color; }""",
            # for OpenGL 2.1 (osx compatibility profile)
            """#version 120
               uniform vec4 rect;
               attribute vec2 st;
               void main() {
                 gl_Position = vec4(rect.x + rect.z*st.x,
                                    rect.y + rect.w*st.y, 0, 1); }""",
            """#version 120
               uniform vec4 color;
               void main() { gl_FragColor = color; }""",
            ["rect", "color"])

        return self._glslProgram

    def glDraw(self, color):
        #don't draw if too small
        if self.xywh[2] < 0.001 or self.xywh[3] < 0.001:
            return

        from OpenGL import GL

        cls = self.__class__

        program = cls.compileProgram()
        if (program.program == 0):
            return

        GL.glUseProgram(program.program)

        if (program._glMajorVersion >= 4):
            GL.glDisable(GL.GL_SAMPLE_ALPHA_TO_COVERAGE)

        # requires PyOpenGL 3.0.2 or later for glGenVertexArrays.
        if (program._glMajorVersion >= 3 and hasattr(GL, 'glGenVertexArrays')):
            if (cls._vao == 0):
                cls._vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(cls._vao)

        # for some reason, we need to bind at least 1 vertex attrib (is OSX)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, cls._vbo)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 0, None)

        program.uniform4f("color", *color)
        program.uniform4f("rect", *self.xywh)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)

class Prim2DSetupTask():
    def __init__(self, viewport):
        self._viewport = viewport[:]

    def Sync(self, ctx):
        pass

    def Execute(self, ctx):
        from OpenGL import GL
        GL.glViewport(*self._viewport)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnable(GL.GL_BLEND)

class Prim2DDrawTask():
    def __init__(self):
        self._prims = []
        self._colors = []

    def Sync(self, ctx):
        for prim in self._prims:
            prim.__class__.compileProgram()

    def Execute(self, ctx):
        from OpenGL import GL
        for prim, color in zip(self._prims, self._colors):
            prim.glDraw(color)

        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        GL.glDisableVertexAttribArray(0)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)

class Outline(Prim2DDrawTask):
    def __init__(self):
        Prim2DDrawTask.__init__(self)
        self._outlineColor = Gf.ConvertDisplayToLinear(Gf.Vec4f(0.0, 0.0, 0.0, 1.0))

    def updatePrims(self, croppedViewport, qglwidget):
        width = float(qglwidget.width())
        height = float(qglwidget.height())
        prims = [ OutlineRect.fromXYWH(croppedViewport) ]
        self._prims = [p.scaledAndBiased((2.0 / width, 2.0 / height), (-1, -1))
                for p in prims]
        self._colors = [ self._outlineColor ]

class Reticles(Prim2DDrawTask):
    def __init__(self):
        Prim2DDrawTask.__init__(self)
        self._outlineColor = Gf.ConvertDisplayToLinear(Gf.Vec4f(0.0, 0.7, 1.0, 0.9))

    def updateColor(self, color):
        self._outlineColor = Gf.ConvertDisplayToLinear(Gf.Vec4f(*color))

    def updatePrims(self, croppedViewport, qglwidget, inside, outside):
        width = float(qglwidget.width())
        height = float(qglwidget.height())
        prims = [ ]
        ascenders = [0, 0]
        descenders = [0, 0]
        if inside:
            descenders = [7, 15]
        if outside:
            ascenders = [7, 15]
        # vertical reticles on the top and bottom
        for i in range(5):
            w = 2.6
            h = ascenders[i & 1] + descenders[i & 1]
            x = croppedViewport[0] - (w / 2) + ((i + 1) * croppedViewport[2]) / 6
            bottomY = croppedViewport[1] - ascenders[i & 1]
            topY = croppedViewport[1] + croppedViewport[3] - descenders[i & 1]
            prims.append(FilledRect.fromXYWH((x, bottomY, w, h)))
            prims.append(FilledRect.fromXYWH((x, topY, w, h)))
        # horizontal reticles on the left and right
        for i in range(5):
            w = ascenders[i & 1] + descenders[i & 1]
            h = 2.6
            leftX = croppedViewport[0] - ascenders[i & 1]
            rightX = croppedViewport[0] + croppedViewport[2] - descenders[i & 1]
            y = croppedViewport[1] - (h / 2) + ((i + 1) * croppedViewport[3]) / 6
            prims.append(FilledRect.fromXYWH((leftX, y, w, h)))
            prims.append(FilledRect.fromXYWH((rightX, y, w, h)))

        self._prims = [p.scaledAndBiased((2.0 / width, 2.0 / height), (-1, -1))
                for p in prims]
        self._colors = [ self._outlineColor ] * len(self._prims)

class Mask(Prim2DDrawTask):
    def __init__(self):
        Prim2DDrawTask.__init__(self)
        self._maskColor = Gf.ConvertDisplayToLinear(Gf.Vec4f(0.0, 0.0, 0.0, 1.0))

    def updateColor(self, color):
        self._maskColor = Gf.ConvertDisplayToLinear(Gf.Vec4f(*color))

    def updatePrims(self, croppedViewport, qglwidget):
        width = float(qglwidget.width())
        height = float(qglwidget.height())
        rect = FilledRect.fromXYWH((0, 0, width, height))
        prims = rect.difference(croppedViewport)
        self._prims = [p.scaledAndBiased((2.0 / width, 2.0 / height), (-1, -1))
                for p in prims]
        self._colors = [ self._maskColor ] * 2

class HUD():
    class Group():
        def __init__(self, name, w, h):
            self.x = 0
            self.y = 0
            self.w = w
            self.h = h
            pixelRatio = QtWidgets.QApplication.instance().devicePixelRatio()
            imageW = w * pixelRatio
            imageH = h * pixelRatio
            self.qimage = QtGui.QImage(imageW, imageH, QtGui.QImage.Format_ARGB32)
            self.qimage.fill(QtGui.QColor(0, 0, 0, 0))
            self.painter = QtGui.QPainter()

    def __init__(self):
        self._pixelRatio = QtWidgets.QApplication.instance().devicePixelRatio()
        self._HUDLineSpacing = 15
        self._HUDFont = QtGui.QFont("Menv Mono Numeric", 9*self._pixelRatio)
        self._groups = {}
        self._glslProgram = None
        self._glMajorVersion = 0
        self._vao = 0

    def compileProgram(self):
        from OpenGL import GL
        import ctypes

        # prep a quad vbo
        self._vbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        st = [0, 0, 1, 0, 0, 1, 1, 1]
        GL.glBufferData(GL.GL_ARRAY_BUFFER, len(st)*4,
                        (ctypes.c_float*len(st))(*st), GL.GL_STATIC_DRAW)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

        self._glslProgram = GLSLProgram(
            # for OpenGL 3.1 or later
            """#version 140
               uniform vec4 rect;
               in vec2 st;
               out vec2 uv;
               void main() {
                 gl_Position = vec4(rect.x + rect.z*st.x,
                                    rect.y + rect.w*st.y, 0, 1);
                 uv          = vec2(st.x, 1 - st.y); }""",
            """#version 140
               in vec2 uv;
               out vec4 color;
               uniform sampler2D tex;
              void main() { color = texture(tex, uv); }""",
            # for OpenGL 2.1 (osx compatibility profile)
            """#version 120
               uniform vec4 rect;
               attribute vec2 st;
               varying vec2 uv;
               void main() {
                 gl_Position = vec4(rect.x + rect.z*st.x,
                                    rect.y + rect.w*st.y, 0, 1);
                 uv          = vec2(st.x, 1 - st.y); }""",
            """#version 120
               varying vec2 uv;
               uniform sampler2D tex;
               void main() { gl_FragColor = texture2D(tex, uv); }""",
            ["rect", "tex"])

        return True

    def addGroup(self, name, w, h):
        self._groups[name] = self.Group(name, w, h)

    def updateGroup(self, name, x, y, col, dic, keys = None):
        group = self._groups[name]
        group.qimage.fill(QtGui.QColor(0, 0, 0, 0))
        group.x = x
        group.y = y
        painter = group.painter
        painter.begin(group.qimage)

        from prettyPrint import prettyPrint
        if keys is None:
            keys = sorted(dic.keys())

        # find the longest key so we know how far from the edge to print
        # add [0] at the end so that max() never gets an empty sequence
        longestKeyLen = max([len(k) for k in dic.iterkeys()]+[0])
        margin = int(longestKeyLen*1.4)

        painter.setFont(self._HUDFont)
        color = QtGui.QColor()
        yy = 10 * self._pixelRatio
        lineSpacing = self._HUDLineSpacing * self._pixelRatio
        for key in keys:
            if not dic.has_key(key):
                continue
            line = key.rjust(margin) + ": " + str(prettyPrint(dic[key]))
            # Shadow of text
            shadow = Gf.ConvertDisplayToLinear(Gf.Vec3f(.2, .2, .2))
            color.setRgbF(shadow[0], shadow[1], shadow[2])
            painter.setPen(color)
            painter.drawText(1, yy+1, line)

            # Colored text
            color.setRgbF(col[0], col[1], col[2])
            painter.setPen(color)
            painter.drawText(0, yy, line)

            yy += lineSpacing

        painter.end()
        return y + lineSpacing

    def draw(self, qglwidget):
        from OpenGL import GL

        if (self._glslProgram == None):
            self.compileProgram()

        if (self._glslProgram.program == 0):
            return

        GL.glUseProgram(self._glslProgram.program)

        width = float(qglwidget.width())
        height = float(qglwidget.height())

        if (self._glslProgram._glMajorVersion >= 4):
            GL.glDisable(GL.GL_SAMPLE_ALPHA_TO_COVERAGE)

        # requires PyOpenGL 3.0.2 or later for glGenVertexArrays.
        if (self._glslProgram._glMajorVersion >= 3 and hasattr(GL, 'glGenVertexArrays')):
            if (self._vao == 0):
                self._vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(self._vao)

        # for some reason, we need to bind at least 1 vertex attrib (is OSX)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 0, None)

        # seems like a bug in Qt4.8/CoreProfile on OSX that GL_UNPACK_ROW_LENGTH has changed.
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, 0)

        for name in self._groups:
            group = self._groups[name]

            tex = qglwidget.bindTexture(group.qimage, GL.GL_TEXTURE_2D, GL.GL_RGBA,
                                        QtOpenGL.QGLContext.NoBindOption)
            GL.glUniform4f(self._glslProgram.uniformLocations["rect"],
                           2*group.x/width - 1,
                           1 - 2*group.y/height - 2*group.h/height,
                           2*group.w/width,
                           2*group.h/height)
            GL.glUniform1i(self._glslProgram.uniformLocations["tex"], 0)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)

            GL.glDeleteTextures(tex)

        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        GL.glDisableVertexAttribArray(0)

        if (self._vao != 0):
            GL.glBindVertexArray(0)

        GL.glUseProgram(0)

class StageView(QtOpenGL.QGLWidget):
    '''
    QGLWidget that displays a USD Stage.  A StageView requires a dataModel
    object from which it will query state it needs to properly image its
    given UsdStage.  See the nested DefaultDataModel class for the expected
    API.
    '''

    # TODO: most, if not all of the state StageView requires (except possibly
    # the stage?), should be migrated to come from the dataModel, and redrawing
    # should be triggered by signals the dataModel emits.
    class DefaultDataModel(QtCore.QObject):

        BBOXPURPOSES = [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy]

        signalDefaultMaterialChanged = QtCore.Signal()

        def __init__(self):
            super(StageView.DefaultDataModel, self).__init__()
            self._bboxCache = UsdGeom.BBoxCache(0,
                                                StageView.DefaultDataModel.BBOXPURPOSES,
                                                useExtentsHint=True)

            self._defaultMaterialAmbient = 0.2
            self._defaultMaterialSpecular = 0.1

            self._defaultFreeCamera = FreeCamera(True)
            self._defaultComplexity = 1.0
            self._defaultDrawSelHighlights = True
            self._defaultShowBBoxes = True
            self._defaultRenderMode = RENDER_MODE_SMOOTH_SHADED
            self._defaultShowHUD = True

        @property
        def bboxCache(self):
            return self._bboxCache

        @property
        def defaultMaterialAmbient(self):
            return self._defaultMaterialAmbient

        @property
        def defaultMaterialSpecular(self):
            return self._defaultMaterialSpecular

        @property
        def cameraMaskColor(self):
            return (0.1, 0.1, 0.1, 1.0)

        @property
        def cameraReticlesColor(self):
            return (0.0, 0.7, 1.0, 1.0)

        @property
        def complexity(self):
            return self._defaultComplexity

        @complexity.setter
        def complexity(self, value):
            self._defaultComplexity = value

        @property
        def clearColor(self):
            return (0.0, 0.0, 0.0, 0.0)

        @property
        def renderMode(self):
            return self._defaultRenderMode

        @renderMode.setter
        def renderMode(self, value):
            self._defaultRenderMode = value

        @property
        def freeCamera(self):
            return self._defaultFreeCamera

        @freeCamera.setter
        def freeCamera(self, value):
            self._defaultFreeCamera = value

        @property
        def playing(self):
            return False

        @property
        def showAABBox(self):
            return True

        @property
        def showOBBox(self):
            return False

        @property
        def showBBoxes(self):
            return self._defaultShowBBoxes

        @showBBoxes.setter
        def showBBoxes(self, value):
            self._defaultShowBBoxes = value

        @property
        def displayGuide(self):
            return False

        @property
        def displayProxy(self):
            return True

        @property
        def displayRender(self):
            return False

        @property
        def displayCameraOracles(self):
            return False

        @property
        def displayPrimId(self):
            return False

        @property
        def enableHardwareShading(self):
            return True

        @property
        def cullBackfaces(self):
            return False

        @property
        def showMask(self):
            return False

        @property
        def showMask_Opaque(self):
            return False

        @property
        def showMask_Outline(self):
            return False

        @property
        def showReticles_Inside(self):
            return False

        @property
        def showReticles_Outside(self):
            return False

        @property
        def showHUD(self):
            return self._defaultShowHUD

        @showHUD.setter
        def showHUD(self, value):
            self._defaultShowHUD = value

        @property
        def showHUD_Info(self):
            return False

        @property
        def showHUD_Complexity(self):
            return True

        @property
        def showHUD_Performance(self):
            return True

        @property
        def showHUD_GPUstats(self):
            return False

        @property
        def ambientLightOnly(self):
            return False

        @property
        def keyLightEnabled(self):
            return True

        @property
        def fillLightEnabled(self):
            return True

        @property
        def backLightEnabled(self):
            return True

        @property
        def highlightColor(self):
            return (1.0,1.0,0.0,0.8) # Yellow

        @property
        def drawSelHighlights(self):
            return self._defaultDrawSelHighlights

        @drawSelHighlights.setter
        def drawSelHighlights(self, value):
            self._defaultDrawSelHighlights = value

    ###########
    # Signals #
    ###########

    signalBboxUpdateTimeChanged = QtCore.Signal(int)

    # First arg is primPath, (which could be empty Path)
    # Second arg is instanceIndex (or UsdImagingGL.GL.ALL_INSTANCES for all instances)
    # Third and Fourth args represent state at time of the pick
    signalPrimSelected = QtCore.Signal(Sdf.Path, int, QtCore.Qt.MouseButton,
                                       QtCore.Qt.KeyboardModifiers)

    # Only raised when StageView has been told to do so, setting
    # rolloverPicking to True
    signalPrimRollover = QtCore.Signal(Sdf.Path, int, QtCore.Qt.KeyboardModifiers)
    signalMouseDrag = QtCore.Signal()
    signalErrorMessage = QtCore.Signal(str)

    signalSwitchedToFreeCam = QtCore.Signal()

    signalFrustumChanged = QtCore.Signal()

    @property
    def currentFrame(self):
        return self._currentFrame

    @property
    def renderParams(self):
        return self._renderParams

    @renderParams.setter
    def renderParams(self, params):
        self._renderParams = params

    @property
    def showReticles(self):
        return ((self._dataModel.showReticles_Inside or self._dataModel.showReticles_Outside)
                and self._cameraPrim != None)

    @property
    def _fitCameraInViewport(self):
       return self._dataModel.showMask or self._dataModel.showMask_Outline or self.showReticles

    @property
    def _cropViewportToCameraViewport(self):
       return self._dataModel.showMask and self._dataModel.showMask_Opaque

    @property
    def cameraPrim(self):
        return self._cameraPrim

    @cameraPrim.setter
    def cameraPrim(self, prim):
        self._cameraPrim = prim

    @property
    def rolloverPicking(self):
        return self._rolloverPicking

    @rolloverPicking.setter
    def rolloverPicking(self, enabled):
        self._rolloverPicking = enabled
        self.setMouseTracking(enabled)

    @property
    def fpsHUDInfo(self):
        return self._fpsHUDInfo

    @fpsHUDInfo.setter
    def fpsHUDInfo(self, info):
        self._fpsHUDInfo = info

    @property
    def fpsHUDKeys(self):
        return self._fpsHUDKeys

    @fpsHUDKeys.setter
    def fpsHUDKeys(self, keys):
        self._fpsHUDKeys = keys

    @property
    def upperHUDInfo(self):
        return self._upperHUDInfo

    @upperHUDInfo.setter
    def upperHUDInfo(self, info):
        self._upperHUDInfo = info

    @property
    def HUDStatKeys(self):
        return self._HUDStatKeys

    @HUDStatKeys.setter
    def HUDStatKeys(self, keys):
        self._HUDStatKeys = keys

    @property
    def overrideNear(self):
        return self._overrideNear

    @overrideNear.setter
    def overrideNear(self, value):
        """To remove the override, set to None.  Causes FreeCamera to become
        active."""
        self._overrideNear = value
        self.switchToFreeCamera()
        self._dataModel.freeCamera.overrideNear = value
        self.updateGL()

    @property
    def overrideFar(self):
        return self._overrideFar

    @overrideFar.setter
    def overrideFar(self, value):
        """To remove the override, set to None.  Causes FreeCamera to become
        active."""
        self._overrideFar = value
        self.switchToFreeCamera()
        self._dataModel.freeCamera.overrideFar = value
        self.updateGL()

    @property
    def allSceneCameras(self):
        return self._allSceneCameras

    @allSceneCameras.setter
    def allSceneCameras(self, value):
        self._allSceneCameras = value

    @property
    def gfCamera(self):
        """Return the last computed Gf Camera"""
        return self._lastComputedGfCamera

    @property
    def cameraFrustum(self):
        """Unlike the StageView.freeCamera property, which is invalid/None
        whenever we are viewing from a scene/stage camera, the 'cameraFrustum'
        property will always return the last-computed camera frustum, regardless
        of source."""
        return self._lastComputedGfCamera.frustum

    def __init__(self, parent=None, dataModel=None):
        self._dataModel = dataModel or StageView.DefaultDataModel()

        self._dataModel.signalDefaultMaterialChanged.connect(self.updateGL)

        glFormat = QtOpenGL.QGLFormat()
        msaa = os.getenv("USDVIEW_ENABLE_MSAA", "1")
        if msaa == "1":
            glFormat.setSampleBuffers(True)
            glFormat.setSamples(4)
        # XXX: for OSX (QT5 required)
        # glFormat.setProfile(QtOpenGL.QGLFormat.CoreProfile)
        super(StageView, self).__init__(glFormat, parent)

        self._dataModel.freeCamera = FreeCamera(True)
        self._lastComputedGfCamera = None

        # prep Mask regions
        self._mask = Mask()
        self._maskOutline = Outline()

        self._reticles = Reticles()

        # prep HUD regions
        self._hud = HUD()
        self._hud.addGroup("TopLeft",     250, 160)  # subtree
        self._hud.addGroup("TopRight",    120, 16)   # Hydra: Enabled
        self._hud.addGroup("BottomLeft",  250, 160)  # GPU stats
        self._hud.addGroup("BottomRight", 200, 32)   # Camera, Complexity

        self._stage = None
        self._stageIsZup = True
        self._currentFrame = 0
        self._cameraMode = "none"
        self._rolloverPicking = False
        self._dragActive = False
        self._lastX = 0
        self._lastY = 0

        self._renderer = None
        self._renderModeDict={RENDER_MODE_WIREFRAME:UsdImagingGL.GL.DrawMode.DRAW_WIREFRAME,
                              RENDER_MODE_WIREFRAME_ON_SURFACE:UsdImagingGL.GL.DrawMode.DRAW_WIREFRAME_ON_SURFACE,
                              RENDER_MODE_SMOOTH_SHADED:UsdImagingGL.GL.DrawMode.DRAW_SHADED_SMOOTH,
                              RENDER_MODE_POINTS:UsdImagingGL.GL.DrawMode.DRAW_POINTS,
                              RENDER_MODE_FLAT_SHADED:UsdImagingGL.GL.DrawMode.DRAW_SHADED_FLAT,
                              RENDER_MODE_GEOM_ONLY:UsdImagingGL.GL.DrawMode.DRAW_GEOM_ONLY,
                              RENDER_MODE_GEOM_SMOOTH:UsdImagingGL.GL.DrawMode.DRAW_GEOM_SMOOTH,
                              RENDER_MODE_GEOM_FLAT:UsdImagingGL.GL.DrawMode.DRAW_GEOM_FLAT,
                              RENDER_MODE_HIDDEN_SURFACE_WIREFRAME:UsdImagingGL.GL.DrawMode.DRAW_WIREFRAME}

        self._renderParams = UsdImagingGL.GL.RenderParams()
        self._defaultFov = 60
        self._dist = 50
        self._oldDist = self._dist
        self._bbox = Gf.BBox3d()
        self._brange = self._bbox.ComputeAlignedRange()
        self._selectionBBox = Gf.BBox3d()
        self._selectionBrange = Gf.Range3d()
        self._selectionOrientedRange = Gf.Range3d()
        self._bbcenterForBoxDraw = (0, 0, 0)
        self._bbcenter = (0,0,0)
        self._rotTheta = 0
        self._rotPhi = 0
        self._oldRotTheta = self._rotTheta
        self._oldRotPhi = self._rotPhi
        self._oldBbCenter = self._bbcenter

        self._overrideNear = None
        self._overrideFar = None

        self._cameraPrim = None
        self._selectedPrims = []

        # blind state of instance selection (key:path, value:indices)
        self._selectedInstances = dict()

        self._forceRefresh = False
        self._renderTime = 0

        self._allSceneCameras = None

        # HUD properties
        self._fpsHUDInfo = dict()
        self._fpsHUDKeys = []
        self._upperHUDInfo = dict()
        self._HUDStatKeys = list()

        self._glPrimitiveGeneratedQuery = None
        self._glTimeElapsedQuery = None

        self._simpleGLSLProgram = None
        self._axisVBO = None
        self._bboxVBO = None
        self._cameraGuidesVBO = None
        self._vao = 0

    def InitRenderer(self):
        '''Create (or re-create) the imager.'''
        self._renderer = UsdImagingGL.GL()
        self._rendererPluginName = ""

    def GetRendererPlugins(self):
        if self._renderer:
            return self._renderer.GetRendererPlugins()
        else:
            return []

    def GetRendererPluginDisplayName(self, plugId):
        if self._renderer:
            return self._renderer.GetRendererPluginDesc(plugId)
        else:
            return ""

    def SetRendererPlugin(self, plugId):
        if self._renderer:
            self._rendererPluginName = self.GetRendererPluginDisplayName(plugId)
            self._renderer.SetRendererPlugin(plugId)

    def GetStage(self):
        return self._stage

    def SetStage(self, stage):
        '''Set the USD Stage this widget will be displaying. To decommission
        (even temporarily) this widget, supply None as 'stage' '''
        if stage is None:
            self._renderer = None
            self._stage = None
            self.allSceneCameras = None
        else:
            self.ReloadStage(stage)
            self._dataModel.freeCamera = FreeCamera(self._stageIsZup)

    def ReloadStage(self, stage):
        self._stage = stage
        # Since this function gets call on startup as well we need to make it
        # does not try to create a renderer because there is no OGL context yet
        if self._renderer != None:
            self.InitRenderer()
        self._stageIsZup = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
        self.allSceneCameras = None

    # simple GLSL program for axis/bbox drawings
    def GetSimpleGLSLProgram(self):
        if self._simpleGLSLProgram == None:
            self._simpleGLSLProgram = GLSLProgram(
            """#version 140
               uniform mat4 mvpMatrix;
               in vec3 position;
               void main() { gl_Position = vec4(position, 1)*mvpMatrix; }""",
            """#version 140
               out vec4 outColor;
               uniform vec4 color;
               void main() { outColor = color; }""",
            """#version 120
               uniform mat4 mvpMatrix;
               attribute vec3 position;
               void main() { gl_Position = vec4(position, 1)*mvpMatrix; }""",
            """#version 120
               uniform vec4 color;
               void main() { gl_FragColor = color; }""",
            ["mvpMatrix", "color"])
        return self._simpleGLSLProgram

    def DrawAxis(self, viewProjectionMatrix):
        from OpenGL import GL
        import ctypes

        # grab the simple shader
        glslProgram = self.GetSimpleGLSLProgram()
        if (glslProgram.program == 0):
            return

        # vao
        if (glslProgram._glMajorVersion >= 3 and hasattr(GL, 'glGenVertexArrays')):
            if (self._vao == 0):
                self._vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(self._vao)

        # prep a vbo for axis
        if (self._axisVBO is None):
            self._axisVBO = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._axisVBO)
            data = [1, 0, 0, 0, 0, 0,
                    0, 1, 0, 0, 0, 0,
                    0, 0, 1, 0, 0, 0]
            GL.glBufferData(GL.GL_ARRAY_BUFFER, len(data)*4,
                            (ctypes.c_float*len(data))(*data), GL.GL_STATIC_DRAW)

        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._axisVBO)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, False, 0, ctypes.c_void_p(0))

        GL.glUseProgram(glslProgram.program)
        mvpMatrix = Gf.Matrix4f().SetScale(self._dist/20.0) * viewProjectionMatrix
        matrix = (ctypes.c_float*16).from_buffer_copy(mvpMatrix)
        GL.glUniformMatrix4fv(glslProgram.uniformLocations["mvpMatrix"],
                              1, GL.GL_TRUE, matrix)

        GL.glUniform4f(glslProgram.uniformLocations["color"], 1, 0, 0, 1)
        GL.glDrawArrays(GL.GL_LINES, 0, 2)
        GL.glUniform4f(glslProgram.uniformLocations["color"], 0, 1, 0, 1)
        GL.glDrawArrays(GL.GL_LINES, 2, 2)
        GL.glUniform4f(glslProgram.uniformLocations["color"], 0, 0, 1, 1)
        GL.glDrawArrays(GL.GL_LINES, 4, 2)

        GL.glDisableVertexAttribArray(0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        GL.glUseProgram(0)

        if (self._vao != 0):
            GL.glBindVertexArray(0)

    def DrawBBox(self, viewProjectionMatrix):
        from OpenGL import GL
        col = self._dataModel.clearColor
        color = Gf.Vec3f(col[0]-.5 if col[0]>0.5 else col[0]+.5,
                         col[1]-.5 if col[1]>0.5 else col[1]+.5,
                         col[2]-.5 if col[2]>0.5 else col[2]+.5)

        # Draw axis-aligned bounding box
        if self._dataModel.showAABBox:
            bsize = self._selectionBrange.max - self._selectionBrange.min

            trans = Gf.Transform()
            trans.SetScale(0.5*bsize)
            trans.SetTranslation(self._bbcenterForBoxDraw)

            self.drawWireframeCube(color,
                                   Gf.Matrix4f(trans.GetMatrix()) * viewProjectionMatrix)

        # Draw oriented bounding box
        if self._dataModel.showOBBox:
            bsize = self._selectionOrientedRange.max - self._selectionOrientedRange.min
            center = bsize / 2. + self._selectionOrientedRange.min
            trans = Gf.Transform()
            trans.SetScale(0.5*bsize)
            trans.SetTranslation(center)

            self.drawWireframeCube(color,
                                   Gf.Matrix4f(trans.GetMatrix()) *
                                   Gf.Matrix4f(self._selectionBBox.matrix) *
                                   viewProjectionMatrix)

    # XXX:
    # First pass at visualizing cameras in usdview-- just oracles for
    # now. Eventually the logic should live in usdImaging, where the delegate
    # would add the camera guide geometry to the GL buffers over the course over
    # its stage traversal, and get time samples accordingly.
    def DrawCameraGuides(self, mvpMatrix):
        from OpenGL import GL
        import ctypes

        # prep a vbo for camera guides
        if (self._cameraGuidesVBO is None):
            self._cameraGuidesVBO = GL.glGenBuffers(1)

        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._cameraGuidesVBO)
        data = []
        for camera in self._allSceneCameras:
            # Don't draw guides for the active camera.
            if camera == self._cameraPrim or not (camera and camera.IsActive()):
                continue

            gfCamera = UsdGeom.Camera(camera).GetCamera(self._currentFrame)
            frustum = gfCamera.frustum

            # (Gf documentation seems to be wrong)-- Ordered as
            # 0: left bottom near
            # 1: right bottom near
            # 2: left top near
            # 3: right top near
            # 4: left bottom far
            # 5: right bottom far
            # 6: left top far
            # 7: right top far
            oraclePoints = frustum.ComputeCorners()

            # Near plane
            indices = [0,1,1,3,3,2,2,0, # Near plane
                       4,5,5,7,7,6,6,4, # Far plane
                       3,7,0,4,1,5,2,6] # Lines between near and far planes.
            data.extend([oraclePoints[i][j] for i in indices for j in range(3)])

        GL.glBufferData(GL.GL_ARRAY_BUFFER, len(data)*4,
                        (ctypes.c_float*len(data))(*data), GL.GL_STATIC_DRAW)

        # grab the simple shader
        glslProgram = self.GetSimpleGLSLProgram()
        if (glslProgram.program == 0):
            return

        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, False, 0, ctypes.c_void_p(0))

        GL.glUseProgram(glslProgram.program)
        matrix = (ctypes.c_float*16).from_buffer_copy(mvpMatrix)
        GL.glUniformMatrix4fv(glslProgram.uniformLocations["mvpMatrix"],
                              1, GL.GL_TRUE, matrix)
        # Grabbed fallback oracleColor from CamCamera.
        GL.glUniform4f(glslProgram.uniformLocations["color"],
                       0.82745, 0.39608, 0.1647, 1)

        GL.glDrawArrays(GL.GL_LINES, 0, len(data)/3)

        GL.glDisableVertexAttribArray(0)
        GL.glUseProgram(0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

    def updateBboxPurposes(self):
        includedPurposes =  set(self._dataModel.bboxCache.GetIncludedPurposes())

        if self._dataModel.displayGuide:
            includedPurposes.add(UsdGeom.Tokens.guide)
        elif UsdGeom.Tokens.guide in includedPurposes:
            includedPurposes.remove(UsdGeom.Tokens.guide)

        if self._dataModel.displayProxy:
            includedPurposes.add(UsdGeom.Tokens.proxy)
        elif UsdGeom.Tokens.proxy in includedPurposes:
            includedPurposes.remove(UsdGeom.Tokens.proxy)

        if self._dataModel.displayRender:
            includedPurposes.add(UsdGeom.Tokens.render)
        elif UsdGeom.Tokens.render in includedPurposes:
            includedPurposes.remove(UsdGeom.Tokens.render)

        self._dataModel.bboxCache.SetIncludedPurposes(includedPurposes)
        # force the bbox to refresh
        self._bbox = Gf.BBox3d()

    def setSelectedPrims(self, selectedPrims, frame, resetCam=False,
                forceComputeBBox=False, frameFit=1.1):
        '''Set the selected prims. resetCam = True causes the camera to reframe
        the specified prims. frameFit sets the ratio of the camera's frustum's
        relevant dimension to the object's bounding box. 1.1, the default,
        fits the prim's bounding box in the frame with a roughly 10% margin.
        '''
        self._selectedPrims = selectedPrims
        self._currentFrame = frame

        # set highlighted paths to renderer
        self._updateSelection()

        # Only compute BBox if forced, if needed for drawing,
        # or if this is the first time running.
        computeBBox = forceComputeBBox or \
                     (self._dataModel.showBBoxes and
                      (self._dataModel.showAABBox or self._dataModel.showOBBox))\
                     or self._bbox.GetRange().IsEmpty()
        if computeBBox:
            try:
                startTime = time()
                self._bbox = self.getStageBBox()
                if len(selectedPrims) == 1 and selectedPrims[0].GetPath() == '/':
                    if self._bbox.GetRange().IsEmpty():
                        self._selectionBBox = self._getDefaultBBox()
                    else:
                        self._selectionBBox = self._bbox
                else:
                    self._selectionBBox = self.getSelectionBBox()

                # BBox computation time for HUD
                endTime = time()
                ms = (endTime - startTime) * 1000.
                self.signalBboxUpdateTimeChanged.emit(ms)

            except RuntimeError:
                # This may fail, but we want to keep the UI available,
                # so print the error and attempt to continue loading
                self.signalErrorMessage.emit("unable to get bounding box on "
                   "stage at frame {0}".format(self._currentFrame))
                import traceback
                traceback.print_exc()
                self._bbox = self._getEmptyBBox()
                self._selectionBBox = self._getDefaultBBox()

        self._brange = self._bbox.ComputeAlignedRange()
        self._selectionBrange = self._selectionBBox.ComputeAlignedRange()
        self._selectionOrientedRange = self._selectionBBox.box
        self._bbcenterForBoxDraw = self._selectionBBox.ComputeCentroid()

        validFrameRange = (not self._selectionBrange.IsEmpty() and
            self._selectionBrange.GetMax() != self._selectionBrange.GetMin())
        if resetCam and validFrameRange:
            self.switchToFreeCamera()
            self._dataModel.freeCamera.frameSelection(self._selectionBBox, frameFit)
            self.computeAndSetClosestDistance()

        self.updateGL()

    def _updateSelection(self):
        psuRoot = self._stage.GetPseudoRoot()
        if not self._renderer:
            return

        self._renderer.ClearSelected()

        for p in self._selectedPrims:
            if p == psuRoot:
                continue
            if self._selectedInstances.has_key(p.GetPath()):
                for instanceIndex in self._selectedInstances[p.GetPath()]:
                    self._renderer.AddSelected(p.GetPath(), instanceIndex)
            else:
                self._renderer.AddSelected(p.GetPath(), UsdImagingGL.GL.ALL_INSTANCES)

    def _getEmptyBBox(self):
        return Gf.BBox3d()

    def _getDefaultBBox(self):
        return Gf.BBox3d(Gf.Range3d((-10,-10,-10), (10,10,10)))

    def getStageBBox(self):
        bbox = self._dataModel.bboxCache.ComputeWorldBound(self._stage.GetPseudoRoot())
        if bbox.GetRange().IsEmpty():
            bbox = self._getEmptyBBox()
        return bbox

    def getSelectionBBox(self):
        bbox = Gf.BBox3d()
        for n in self._selectedPrims:
            if n.IsActive() and not n.IsInMaster():
                primBBox = self._dataModel.bboxCache.ComputeWorldBound(n)
                bbox = Gf.BBox3d.Combine(bbox, primBBox)
        return bbox

    def getCameraPrim(self):
        return self._cameraPrim

    def setCameraPrim(self, cameraPrim):
        if not cameraPrim:
            self.switchToFreeCamera()
            return

        if cameraPrim.IsA(UsdGeom.Camera):
            self._dataModel.freeCamera = None
            self._cameraPrim = cameraPrim
        else:
            from common import PrintWarning
            PrintWarning("Incorrect Prim Type",
                         "Attempted to view the scene using the prim '%s', but "
                         "the prim is not a UsdGeom.Camera." %(cameraPrim.GetName()))

    def renderSinglePass(self, renderMode, renderSelHighlights):
        if not self._stage or not self._renderer:
            return

        # update rendering parameters
        self._renderParams.frame = self._currentFrame
        self._renderParams.complexity = self._dataModel.complexity
        self._renderParams.drawMode = renderMode
        self._renderParams.showGuides = self._dataModel.displayGuide
        self._renderParams.showProxy = self._dataModel.displayProxy
        self._renderParams.showRender = self._dataModel.displayRender
        self._renderParams.forceRefresh = self._forceRefresh
        self._renderParams.cullStyle =  (UsdImagingGL.GL.CullStyle.CULL_STYLE_BACK_UNLESS_DOUBLE_SIDED
                                               if self._dataModel.cullBackfaces
                                               else UsdImagingGL.GL.CullStyle.CULL_STYLE_NOTHING)
        self._renderParams.gammaCorrectColors = False
        self._renderParams.enableIdRender = self._dataModel.displayPrimId
        self._renderParams.enableSampleAlphaToCoverage = not self._dataModel.displayPrimId
        self._renderParams.highlight = renderSelHighlights
        self._renderParams.enableHardwareShading = self._dataModel.enableHardwareShading

        pseudoRoot = self._stage.GetPseudoRoot()

        self._renderer.SetSelectionColor(self._dataModel.highlightColor)
        self._renderer.Render(pseudoRoot, self._renderParams)
        self._forceRefresh = False


    def initializeGL(self):
        if not self.context():
            return
        from pxr import Glf
        if not Glf.GlewInit():
            return
        Glf.RegisterDefaultDebugOutputMessageCallback()
        # Initialize the renderer now since the context is available
        self.InitRenderer()

    def updateGL(self):
        """We override this virtual so that we can make it a no-op during
        playback.  The client driving playback at a particular rate should
        instead call updateForPlayback() to image the next frame."""
        if not self._dataModel.playing:
            super(StageView, self).updateGL()

    def updateForPlayback(self, currentTime, showHighlights):
        """If playing, update the GL canvas.  Otherwise a no-op"""
        if self._dataModel.playing:
            self._currentFrame = currentTime
            drawHighlights = self._dataModel.drawSelHighlights
            self._dataModel.drawSelHighlights = showHighlights
            super(StageView, self).updateGL()
            self._dataModel.drawSelHighlights = drawHighlights

    def computeGfCameraForCurrentPrim(self):
        if self._cameraPrim and self._cameraPrim.IsActive():
            gfCamera = UsdGeom.Camera(self._cameraPrim).GetCamera(
                self._currentFrame)
            return gfCamera
        else:
            return None

    def computeSize(self):
         size = self.size() * QtWidgets.QApplication.instance().devicePixelRatio()
         return (int(size.width()), int(size.height()))

    def computeViewport(self):
        return (0, 0) + self.computeSize()

    def computeGfCameraAndViewport(self):
        windowPolicy = CameraUtil.MatchVertically
        targetAspect = (
          float(self.size().width()) / max(1.0, self.size().height()))
        conformCameraWindow = True

        camera = self.computeGfCameraForCurrentPrim()
        if not camera:
            # If 'camera' is None, make sure we have a valid freeCamera
            self.switchToFreeCamera()
            camera = self._dataModel.freeCamera.computeGfCamera(self._bbox)
        elif self._fitCameraInViewport:
            if targetAspect < camera.aspectRatio:
                windowPolicy = CameraUtil.MatchHorizontally
            conformCameraWindow = False

        if conformCameraWindow:
            CameraUtil.ConformWindow(camera, windowPolicy, targetAspect)

        viewport = Gf.Range2d(Gf.Vec2d(0, 0),
                              Gf.Vec2d(self.computeSize()))
        viewport = CameraUtil.ConformedWindow(viewport, windowPolicy, camera.aspectRatio)

        frustumChanged = ((not self._lastComputedGfCamera) or
                          self._lastComputedGfCamera.frustum != camera.frustum)
        # We need to COPY the camera, not assign it...
        self._lastComputedGfCamera = Gf.Camera(camera)
        if frustumChanged:
            self.signalFrustumChanged.emit()
        return (camera, (viewport.GetMin()[0], viewport.GetMin()[1],
                         viewport.GetSize()[0], viewport.GetSize()[1]))

    def copyViewState(self):
        """Returns a copy of this StageView's view-affecting state,
        which can be used later to restore the view via restoreViewState().
        Take note that we do NOT include the StageView's notion of the
        current time (used by prim-based cameras to extract their data),
        since we do not want a restore operation to put us out of sync
        with respect to our owner's time.
        """
        viewState = {}
        for attr in ["_cameraPrim", "_stageIsZup",
                     "_overrideNear", "_overrideFar" ]:
            viewState[attr] = getattr(self, attr)
        # Since FreeCamera is a compound/class object, we must copy
        # it more deeply
        viewState["_freeCamera"] = self._dataModel.freeCamera.clone() if self._dataModel.freeCamera else None
        return viewState

    def restoreViewState(self, viewState):
        """Restore view parameters from 'viewState', and redraw"""
        for key,val in viewState.iteritems():
            setattr(self, key, val)
        # Detach our freeCamera from the given viewState, to
        # insulate against changes to viewState by caller
        if viewState.has_key("_freeCamera") and self._dataModel.freeCamera:
            self._dataModel.freeCamera = self._dataModel.freeCamera.clone()
        self.update()

    def drawWireframeCube(self, col, mvpMatrix):
        from OpenGL import GL
        import ctypes, itertools

        # grab the simple shader
        glslProgram = self.GetSimpleGLSLProgram()
        if (glslProgram.program == 0):
            return
        # vao
        if (glslProgram._glMajorVersion >= 3 and hasattr(GL, 'glGenVertexArrays')):
            if (self._vao == 0):
                self._vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(self._vao)

        # prep a vbo for bbox
        if (self._bboxVBO is None):
            self._bboxVBO = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._bboxVBO)
            # create 12 edges
            data = []
            p = list(itertools.product([-1,1],[-1,1],[-1,1]))
            for i in p:
                data.extend([i[0], i[1], i[2]])
            for i in p:
                data.extend([i[1], i[2], i[0]])
            for i in p:
                data.extend([i[2], i[0], i[1]])

            GL.glBufferData(GL.GL_ARRAY_BUFFER, len(data)*4,
                            (ctypes.c_float*len(data))(*data), GL.GL_STATIC_DRAW)

        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._bboxVBO)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, False, 0, ctypes.c_void_p(0))

        GL.glEnable(GL.GL_LINE_STIPPLE)
        GL.glLineStipple(2,0xAAAA)

        GL.glUseProgram(glslProgram.program)
        matrix = (ctypes.c_float*16).from_buffer_copy(mvpMatrix)
        GL.glUniformMatrix4fv(glslProgram.uniformLocations["mvpMatrix"],
                              1, GL.GL_TRUE, matrix)
        GL.glUniform4f(glslProgram.uniformLocations["color"],
                       col[0], col[1], col[2], 1)

        GL.glDrawArrays(GL.GL_LINES, 0, 24)

        GL.glDisableVertexAttribArray(0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        GL.glUseProgram(0)

        GL.glDisable(GL.GL_LINE_STIPPLE)
        if (self._vao != 0):
            GL.glBindVertexArray(0)

    def paintGL(self):
        if not self._stage or not self._renderer:
            return

        from OpenGL import GL
        from OpenGL import GLU

        if self._dataModel.showHUD_GPUstats:
            if self._glPrimitiveGeneratedQuery is None:
                self._glPrimitiveGeneratedQuery = Glf.GLQueryObject()
            if self._glTimeElapsedQuery is None:
                self._glTimeElapsedQuery = Glf.GLQueryObject()
            self._glPrimitiveGeneratedQuery.BeginPrimitivesGenerated()
            self._glTimeElapsedQuery.BeginTimeElapsed()

        # Enable sRGB in order to apply a final gamma to this window, just like
        # in Presto.
        from OpenGL.GL.EXT.framebuffer_sRGB import GL_FRAMEBUFFER_SRGB_EXT
        GL.glEnable(GL_FRAMEBUFFER_SRGB_EXT)

        GL.glClearColor(*(Gf.ConvertDisplayToLinear(Gf.Vec4f(self._dataModel.clearColor))))

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LESS)

        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnable(GL.GL_BLEND)

        (gfCamera, cameraViewport) = self.computeGfCameraAndViewport()
        frustum = gfCamera.frustum

        viewport = self.computeViewport()
        cameraViewport = ViewportMakeCenteredIntegral(cameraViewport)
        if self._fitCameraInViewport:
            if self._cropViewportToCameraViewport:
                viewport = cameraViewport
            else:
                windowAspect = float(viewport[2]) / max(1.0, viewport[3])
                CameraUtil.ConformWindow(frustum, CameraUtil.Fit, windowAspect)

        cam_pos = frustum.position
        cam_up = frustum.ComputeUpVector()
        cam_right = Gf.Cross(frustum.ComputeViewDirection(), cam_up)

        self._renderer.SetCameraState(
            frustum.ComputeViewMatrix(),
            frustum.ComputeProjectionMatrix(),
            Gf.Vec4d(*viewport))

        viewProjectionMatrix = Gf.Matrix4f(frustum.ComputeViewMatrix()
                                           * frustum.ComputeProjectionMatrix())


        GL.glViewport(*viewport)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT|GL.GL_DEPTH_BUFFER_BIT)

        # ensure viewport is right for the camera framing
        GL.glViewport(*viewport)

        # Set the clipping planes.
        self._renderParams.clipPlanes = [Gf.Vec4d(i) for i in
                                         gfCamera.clippingPlanes]

        if self._selectedPrims:
            sceneAmbient = (0.01, 0.01, 0.01, 1.0)
            material = Glf.SimpleMaterial()
            lights = []
            # for renderModes that need lights
            if self._dataModel.renderMode in (RENDER_MODE_FLAT_SHADED,
                                              RENDER_MODE_SMOOTH_SHADED,
                                              RENDER_MODE_WIREFRAME_ON_SURFACE,
                                              RENDER_MODE_GEOM_SMOOTH,
                                              RENDER_MODE_GEOM_FLAT):

                stagePos = Gf.Vec3d(self._bbcenter[0], self._bbcenter[1],
                                    self._bbcenter[2])
                stageDir = (stagePos - cam_pos).GetNormalized()

                # ambient light located at the camera
                if self._dataModel.ambientLightOnly:
                    l = Glf.SimpleLight()
                    l.ambient = (0, 0, 0, 0)
                    l.position = (cam_pos[0], cam_pos[1], cam_pos[2], 1)
                    lights.append(l)
                # three-point lighting
                else:
                    if self._dataModel.keyLightEnabled:
                        # 45 degree horizontal viewing angle, 20 degree vertical
                        keyHorz = -1 / tan(rad(45)) * cam_right
                        keyVert = 1 / tan(rad(70)) * cam_up
                        keyPos = cam_pos + (keyVert + keyHorz) * self._dist
                        keyColor = (.8, .8, .8, 1.0)

                        l = Glf.SimpleLight()
                        l.ambient = (0, 0, 0, 0)
                        l.diffuse = keyColor
                        l.specular = keyColor
                        l.position = (keyPos[0], keyPos[1], keyPos[2], 1)
                        lights.append(l)

                    if self._dataModel.fillLightEnabled:
                        # 60 degree horizontal viewing angle, 45 degree vertical
                        fillHorz = 1 / tan(rad(30)) * cam_right
                        fillVert = 1 / tan(rad(45)) * cam_up
                        fillPos = cam_pos + (fillVert + fillHorz) * self._dist
                        fillColor = (.6, .6, .6, 1.0)

                        l = Glf.SimpleLight()
                        l.ambient = (0, 0, 0, 0)
                        l.diffuse = fillColor
                        l.specular = fillColor
                        l.position = (fillPos[0], fillPos[1], fillPos[2], 1)
                        lights.append(l)

                    if self._dataModel.backLightEnabled:
                        # back light base is camera position refelcted over origin
                        # 30 degree horizontal viewing angle, 30 degree vertical
                        backPos = cam_pos + (stagePos - cam_pos) * 2
                        backHorz = 1 / tan(rad(60)) * cam_right
                        backVert = -1 / tan(rad(60)) * cam_up
                        backPos += (backHorz + backVert) * self._dist
                        backColor = (.6, .6, .6, 1.0)

                        l = Glf.SimpleLight()
                        l.ambient = (0, 0, 0, 0)
                        l.diffuse = backColor
                        l.specular = backColor
                        l.position = (backPos[0], backPos[1], backPos[2], 1)
                        lights.append(l)

                kA = self._dataModel.defaultMaterialAmbient
                kS = self._dataModel.defaultMaterialSpecular
                material.ambient = (kA, kA, kA, 1.0)
                material.specular = (kS, kS, kS, 1.0)
                material.shininess = 32.0

            # modes that want no lighting simply leave lights as an empty list
            self._renderer.SetLightingState(lights, material, sceneAmbient)

            if self._dataModel.renderMode == RENDER_MODE_HIDDEN_SURFACE_WIREFRAME:
                GL.glEnable( GL.GL_POLYGON_OFFSET_FILL )
                GL.glPolygonOffset( 1.0, 1.0 )
                GL.glPolygonMode( GL.GL_FRONT_AND_BACK, GL.GL_FILL )

                self.renderSinglePass( self._renderer.DrawMode.DRAW_GEOM_ONLY,
                                       False)

                GL.glDisable( GL.GL_POLYGON_OFFSET_FILL )
                GL.glDepthFunc(GL.GL_LEQUAL)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT)

            self.renderSinglePass(self._renderModeDict[self._dataModel.renderMode],
                                  self._dataModel.drawSelHighlights)

            self.DrawAxis(viewProjectionMatrix)

            # XXX:
            # Draw camera guides-- no support for toggling guide visibility on
            # individual cameras until we move this logic directly into
            # usdImaging.
            if self._dataModel.displayCameraOracles:
                self.DrawCameraGuides(viewProjectionMatrix)

            if self._dataModel.showBBoxes:
                self.DrawBBox(viewProjectionMatrix)
        else:
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        if self._dataModel.showHUD_GPUstats:
            self._glPrimitiveGeneratedQuery.End()
            self._glTimeElapsedQuery.End()

        # reset the viewport for 2D and HUD drawing
        uiTasks = [ Prim2DSetupTask(self.computeViewport()) ]
        if self._dataModel.showMask:
            color = self._dataModel.cameraMaskColor
            if self._dataModel.showMask_Opaque:
                color = color[0:3] + (1.0,)
            else:
                color = color[0:3] + (color[3] * 0.7,)
            self._mask.updateColor(color)
            self._mask.updatePrims(cameraViewport, self)
            uiTasks.append(self._mask)
        if self._dataModel.showMask_Outline:
            self._maskOutline.updatePrims(cameraViewport, self)
            uiTasks.append(self._maskOutline)
        if self.showReticles:
            color = self._dataModel.cameraReticlesColor
            color = color[0:3] + (color[3] * 0.85,)
            self._reticles.updateColor(color)
            self._reticles.updatePrims(cameraViewport, self,
                    self._dataModel.showReticles_Inside, self._dataModel.showReticles_Outside)
            uiTasks.append(self._reticles)

        for task in uiTasks:
            task.Sync(None)
        for task in uiTasks:
            task.Execute(None)

        # ### DRAW HUD ### #
        if self._dataModel.showHUD:
            self.drawHUD()

        GL.glDisable(GL_FRAMEBUFFER_SRGB_EXT)

        if (not self._dataModel.playing) & (not self._renderer.IsConverged()):
            QtCore.QTimer.singleShot(5, self.update)

    def drawHUD(self):
        # compute the time it took to render this frame,
        # so we can display it in the HUD
        ms = self._renderTime * 1000.
        fps = float("inf")
        if not self._renderTime == 0:
            fps = 1./self._renderTime
        # put the result in the HUD string
        self.fpsHUDInfo['Render'] = "%.2f ms (%.2f FPS)" % (ms, fps)

        col = Gf.ConvertDisplayToLinear(Gf.Vec3f(.733,.604,.333))

        # the subtree info does not update while animating, grey it out
        if not self._dataModel.playing:
            subtreeCol = col
        else:
            subtreeCol = Gf.ConvertDisplayToLinear(Gf.Vec3f(.6,.6,.6))

        # Subtree Info
        if self._dataModel.showHUD_Info:
            self._hud.updateGroup("TopLeft", 0, 14, subtreeCol,
                                 self.upperHUDInfo,
                                 self.HUDStatKeys)
        else:
            self._hud.updateGroup("TopLeft", 0, 0, subtreeCol, {})

        # Complexity
        if self._dataModel.showHUD_Complexity:
            # Camera name
            camName = "Free"
            if self._cameraPrim:
                camName = self._cameraPrim.GetName()

            toPrint = {"Complexity" : self._dataModel.complexity,
                       "Camera" : camName}
            self._hud.updateGroup("BottomRight",
                                  self.width()-200, self.height()-self._hud._HUDLineSpacing*2,
                                  col, toPrint)
        else:
            self._hud.updateGroup("BottomRight", 0, 0, col, {})

        # Hydra Enabled (Top Right)
        hydraMode = "Disabled"

        if UsdImagingGL.GL.IsEnabledHydra():
            hydraMode = self._rendererPluginName
            if not hydraMode:
                hydraMode = "Enabled"

        toPrint = {"Hydra": hydraMode}
        self._hud.updateGroup("TopRight", self.width()-140, 14, col, toPrint)

        # bottom left
        from collections import OrderedDict
        toPrint = OrderedDict()

        # GPU stats (TimeElapsed is in nano seconds)
        if self._dataModel.showHUD_GPUstats:
            allocInfo = self._renderer.GetResourceAllocation()
            gpuMemTotal = 0
            texMem = 0
            if "gpuMemoryUsed" in allocInfo:
                gpuMemTotal = allocInfo["gpuMemoryUsed"]
            if "textureMemoryUsed" in allocInfo:
                texMem = allocInfo["textureMemoryUsed"]
                gpuMemTotal += texMem

            toPrint["GL prims "] = self._glPrimitiveGeneratedQuery.GetResult()
            toPrint["GPU time "] = "%.2f ms " % (self._glTimeElapsedQuery.GetResult() / 1000000.0)
            toPrint["GPU mem  "] = gpuMemTotal
            toPrint[" primvar "] = allocInfo["primVar"] if "primVar" in allocInfo else "N/A"
            toPrint[" topology"] = allocInfo["topology"] if "topology" in allocInfo else "N/A"
            toPrint[" shader  "] = allocInfo["drawingShader"] if "drawingShader" in allocInfo else "N/A"
            toPrint[" texture "] = texMem

        # Playback Rate
        if self._dataModel.showHUD_Performance:
            for key in self.fpsHUDKeys:
                toPrint[key] = self.fpsHUDInfo[key]
        if len(toPrint) > 0:
            self._hud.updateGroup("BottomLeft",
                                  0, self.height()-len(toPrint)*self._hud._HUDLineSpacing,
                                  col, toPrint, toPrint.keys())

        # draw HUD
        self._hud.draw(self)

    def sizeHint(self):
        return QtCore.QSize(460, 460)

    def switchToFreeCamera(self, computeAndSetClosestDistance=True):
        """
        If our current camera corresponds to a prim, create a FreeCamera
        that has the same view and use it.
        """
        if self._cameraPrim != None:
            # _cameraPrim may no longer be valid, so use the last-computed
            # gf camera
            if self._lastComputedGfCamera:
                self._dataModel.freeCamera = FreeCamera.FromGfCamera(self._lastComputedGfCamera, self._stageIsZup)
            else:
                self._dataModel.freeCamera = FreeCamera(self._stageIsZup)
            # override clipping plane state is managed by StageView,
            # so that it can be persistent.  Therefore we must restore it
            # now
            self._dataModel.freeCamera.overrideNear = self._overrideNear
            self._dataModel.freeCamera.overrideFar = self._overrideFar
            self._cameraPrim = None
            if computeAndSetClosestDistance:
                self.computeAndSetClosestDistance()
            # let the controller know we've done this!
            self.signalSwitchedToFreeCam.emit()

    # It WBN to support marquee selection in the viewer also, at some point...
    def mousePressEvent(self, event):
        """This widget claims the Alt modifier key as the enabler for camera
        manipulation, and will consume mousePressEvents when Alt is present.
        In any other modifier state, a mousePressEvent will result in a
        pick operation, and the pressed button and active modifiers will be
        made available to clients via a signalPrimSelected()."""

        # It's important to set this first, since pickObject(), called below
        # may produce the mouse-up event that will terminate the drag
        # initiated by this mouse-press
        self._dragActive = True

        if (event.modifiers() & QtCore.Qt.AltModifier):
            if event.button() == QtCore.Qt.LeftButton:
                self.switchToFreeCamera()
                self._cameraMode = "tumble"
            if event.button() == QtCore.Qt.MidButton:
                self.switchToFreeCamera()
                self._cameraMode = "truck"
            if event.button() == QtCore.Qt.RightButton:
                self.switchToFreeCamera()
                self._cameraMode = "zoom"
        else:
            self._cameraMode = "pick"
            self.pickObject(event.x(), event.y(),
                            event.button(), event.modifiers())

        self._lastX = event.x()
        self._lastY = event.y()

    def mouseReleaseEvent(self, event):
        self._cameraMode = "none"
        self._dragActive = False

    def mouseMoveEvent(self, event ):

        if self._dragActive:
            dx = event.x() - self._lastX
            dy = event.y() - self._lastY
            if dx == 0 and dy == 0:
                return
            if self._cameraMode == "tumble":
                self._dataModel.freeCamera.rotTheta += 0.25 * dx
                self._dataModel.freeCamera.rotPhi += 0.25 * dy

            elif self._cameraMode == "zoom":
                zoomDelta = -.002 * (dx + dy)
                self._dataModel.freeCamera.adjustDist(1 + zoomDelta)

            elif self._cameraMode == "truck":
                height = float(self.size().height())
                self._dataModel.freeCamera.Truck(dx, dy, height)

            self._lastX = event.x()
            self._lastY = event.y()
            self.updateGL()

            self.signalMouseDrag.emit()
        elif self._cameraMode == "none":
            # Mouse tracking is only enabled when rolloverPicking is enabled,
            # and this function only gets called elsewise when mouse-tracking
            # is enabled
            self.pickObject(event.x(), event.y(), None, event.modifiers())
        else:
            event.ignore()

    def wheelEvent(self, event):
        distBefore = self._dist
        self.switchToFreeCamera()
        self._dataModel.freeCamera.adjustDist(1-max(-0.5,min(0.5,(event.angleDelta().y()/1000.))))
        self.updateGL()

    def detachAndReClipFromCurrentCamera(self):
        """If we are currently rendering from a prim camera, switch to the
        FreeCamera.  Then reset the near/far clipping planes based on
        distance to closest geometry."""
        if not self._dataModel.freeCamera:
            self.switchToFreeCamera()
        else:
            self.computeAndSetClosestDistance()

    def computeAndSetClosestDistance(self):
        '''Using the current FreeCamera's frustum, determine the world-space
        closest rendered point to the camera.  Use that point
        to set our FreeCamera's closest visible distance.'''
        # pick() operates at very low screen resolution, but that's OK for
        # our purposes.  Ironically, the same limited Z-buffer resolution for
        # which we are trying to compensate may cause us to completely lose
        # ALL of our geometry if we set the near-clip really small (which we
        # want to do so we don't miss anything) when geometry is clustered
        # closer to far-clip.  So in the worst case, we may need to perform
        # two picks, with the first pick() using a small near and far, and the
        # second pick() using a near that keeps far within the safe precision
        # range.  We don't expect the worst-case to happen often.
        if not self._dataModel.freeCamera:
            return
        cameraFrustum = self.computeGfCameraAndViewport()[0].frustum
        trueFar = cameraFrustum.nearFar.max
        smallNear = min(FreeCamera.defaultNear,
                        self._dataModel.freeCamera._selSize / 10.0)
        cameraFrustum.nearFar = \
            Gf.Range1d(smallNear, smallNear*FreeCamera.maxSafeZResolution)
        scrSz = self.size()
        pickResults = self.pick(cameraFrustum)
        if pickResults[0] is None or pickResults[1] == Sdf.Path.emptyPath:
            cameraFrustum.nearFar = \
                Gf.Range1d(trueFar/FreeCamera.maxSafeZResolution, trueFar)
            pickResults = self.pick(cameraFrustum)
            if Tf.Debug.IsDebugSymbolNameEnabled(DEBUG_CLIPPING):
                print "computeAndSetClosestDistance: Needed to call pick() a second time"

        if pickResults[0] is not None and pickResults[1] != Sdf.Path.emptyPath:
            self._dataModel.freeCamera.setClosestVisibleDistFromPoint(pickResults[0])
            self.updateGL()

    def pick(self, pickFrustum):
        '''
        Find closest point in scene rendered through 'pickFrustum'.
        Returns a quintuple:
          selectedPoint, selectedPrimPath, selectedInstancerPath,
          selectedInstanceIndex, selectedElementIndex
        '''
        if not self._stage or not self._renderer:
            return None, Sdf.Path.emptyPath, None, None, None

        from OpenGL import GL

        # Need a correct OpenGL Rendering context for FBOs
        self.makeCurrent()

        # update rendering parameters
        self._renderParams.frame = self._currentFrame
        self._renderParams.complexity = self._dataModel.complexity
        self._renderParams.drawMode = self._renderModeDict[self._dataModel.renderMode]
        self._renderParams.showGuides = self._dataModel.displayGuide
        self._renderParams.showProxy = self._dataModel.displayProxy
        self._renderParams.showRender = self._dataModel.displayRender
        self._renderParams.forceRefresh = self._forceRefresh
        self._renderParams.cullStyle =  (UsdImagingGL.GL.CullStyle.CULL_STYLE_BACK_UNLESS_DOUBLE_SIDED
                                               if self._dataModel.cullBackfaces
                                               else UsdImagingGL.GL.CullStyle.CULL_STYLE_NOTHING)
        self._renderParams.gammaCorrectColors = False
        self._renderParams.enableIdRender = True
        self._renderParams.enableSampleAlphaToCoverage = False
        self._renderParams.enableHardwareShading = self._dataModel.enableHardwareShading

        results = self._renderer.TestIntersection(
                pickFrustum.ComputeViewMatrix(),
                pickFrustum.ComputeProjectionMatrix(),
                Gf.Matrix4d(1.0),
                self._stage.GetPseudoRoot(), self._renderParams)
        if Tf.Debug.IsDebugSymbolNameEnabled(DEBUG_CLIPPING):
            print "Pick results = {}".format(results)
        return results

    def computePickFrustum(self, x, y):

        # normalize position and pick size by the viewport size
        width, height = self.computeSize()
        size = Gf.Vec2d(1.0 / width, 1.0 / height)

        # compute pick frustum
        cameraFrustum = self.computeGfCameraAndViewport()[0].frustum

        return cameraFrustum.ComputeNarrowedFrustum(
            Gf.Vec2d((2.0 * x) / width - 1.0,
                     (2.0 * (height-y)) / height - 1.0),
            size)

    def pickObject(self, x, y, button, modifiers):
        '''
        Render stage into fbo with each piece as a different color.
        Emits a signalPrimSelected or signalRollover depending on
        whether 'button' is None.
        '''
        if not self._stage:
            return

        selectedPoint, selectedPrimPath, selectedInstancerPath, selectedInstanceIndex, selectedElementIndex = \
            self.pick(self.computePickFrustum(x, y))

        # The call to TestIntersection will return the path to a master prim
        # (selectedPrimPath) and its instancer (selectedInstancerPath) if the prim is
        # instanced.
        # Figure out which instance was actually picked and use that as our selection
        # in this case.
        if selectedInstancerPath:
            instancePrimPath, absInstanceIndex = self._renderer.GetPrimPathFromInstanceIndex(
                selectedPrimPath, selectedInstanceIndex)
            if instancePrimPath:
                selectedPrimPath = instancePrimPath
                selectedInstanceIndex = absInstanceIndex
        else:
            selectedInstanceIndex = UsdImagingGL.GL.ALL_INSTANCES

        selectedPrim = self._stage.GetPrimAtPath(selectedPrimPath)

        if button:
            self.signalPrimSelected.emit(selectedPrimPath, selectedInstanceIndex, button, modifiers)
        else:
            self.signalPrimRollover.emit(selectedPrimPath, selectedInstanceIndex, modifiers)

    def clearInstanceSelection(self):
        self._selectedInstances.clear()

    def setInstanceSelection(self, path, instanceIndex, selected):
        if selected:
            if not self._selectedInstances.has_key(path):
                self._selectedInstances[path] = set()
            if instanceIndex == UsdImagingGL.GL.ALL_INSTANCES:
                del self._selectedInstances[path]
            else:
                self._selectedInstances[path].add(instanceIndex)
        else:
            if self._selectedInstances.has_key(path):
                self._selectedInstances[path].remove(instanceIndex)
                if len(self._selectedInstances[path]) == 0:
                    del self._selectedInstances[path]

    def getSelectedInstanceIndices(self, path):
        if self._selectedInstances.has_key(path):
            return self._selectedInstances[path]
        return set()

    def getInstanceSelection(self, path, instanceIndex):
        if instanceIndex in self.getSelectedInstanceIndices(path):
            return True
        return False

    def glDraw(self):
        # override glDraw so we can time it.
        startTime = time()
        if self._renderer:
            QtOpenGL.QGLWidget.glDraw(self)
        self._renderTime = time() - startTime

    def SetForceRefresh(self, val):
        self._forceRefresh = val or self._forceRefresh

    def ExportFreeCameraToStage(self, stage, defcamName='usdviewCam',
                                imgWidth=None, imgHeight=None):
        '''
        Export the free camera to the specified USD stage, if it is
        currently defined. If it is not active (i.e. we are viewing through
        a stage camera), raise a ValueError.
        '''
        if not self._dataModel.freeCamera:
            raise ValueError("StageView's Free Camera is not defined, so cannot"
                             " be exported")

        imgWidth = imgWidth if imgWidth is not None else self.width()
        imgHeight = imgHeight if imgHeight is not None else self.height()

        defcam = UsdGeom.Camera.Define(stage, '/'+defcamName)

        # Map free camera params to usd camera.
        gfCamera = self._dataModel.freeCamera.computeGfCamera(self._bbox)

        targetAspect = float(imgWidth) / max(1.0, imgHeight)
        CameraUtil.ConformWindow(
            gfCamera, CameraUtil.MatchVertically, targetAspect)

        when = self._currentFrame if stage.HasAuthoredTimeCodeRange() \
            else Usd.TimeCode.Default()

        defcam.SetFromCamera(gfCamera, when)

    def ExportSession(self, stagePath, defcamName='usdviewCam',
                      imgWidth=None, imgHeight=None):
        '''
        Export the free camera (if currently active) and session layer to a
        USD file at the specified stagePath that references the current-viewed
        stage.
        '''

        tmpStage = Usd.Stage.CreateNew(stagePath)
        if self._stage:
            tmpStage.GetRootLayer().TransferContent(
                self._stage.GetSessionLayer())

        if not self.cameraPrim:
            # Export the free camera if it's the currently-visible camera
            self.ExportFreeCameraToStage(tmpStage, defcamName, imgWidth,
                imgHeight)

        tmpStage.GetRootLayer().Save()
        del tmpStage

        # Reopen just the tmp layer, to sublayer in the pose cache without
        # incurring Usd composition cost.
        if self._stage:
            from pxr import Sdf
            sdfLayer = Sdf.Layer.FindOrOpen(stagePath)
            sdfLayer.subLayerPaths.append(
                os.path.abspath(self._stage.GetRootLayer().realPath))
            sdfLayer.Save()

