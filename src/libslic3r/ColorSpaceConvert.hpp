#ifndef slic3r_ColorSpaceConvert_hpp_
#define slic3r_ColorSpaceConvert_hpp_

#include <tuple>

// Pure colour-space arithmetic, with no toolkit dependency.
//
// This used to live entirely in `src/slic3r/Utils/ColorSpaceConvert.cpp`,
// which also declares two wxWidgets helpers and therefore includes
// <wx/colordlg.h>. `libslic3r/FlushVolCalc.cpp` nonetheless reached up into
// that GUI header for RGB2HSV, so anything linking FlushVolCalculator without
// wxWidgets — the headless worker — failed to link. Splitting the maths down
// into libslic3r removes that layering inversion; the wx helpers stay in the
// GUI layer, whose header includes this one so its own callers are unchanged.
// The implementations are moved verbatim, so no colour ever converts
// differently than it did before.

std::tuple<int, int, int> rgb_to_yuv(float r, float g, float b);
double PivotRGB(double n);
double PivotXYZ(double n);
void XYZ2RGB(float X, float Y, float Z, float* R, float* G, float* B);
void Lab2XYZ(float L, float a, float b, float* X, float* Y, float* Z);
void Lab2RGB(float L, float a, float b, float* R, float* G, float* B);
void RGB2XYZ(float R, float G, float B, float* X, float* Y, float* Z);
void XYZ2Lab(float X, float Y, float Z, float* L, float* a, float* b);
void RGB2Lab(float R, float G, float B, float* L, float* a, float* b);
void RGB2HSV(float r, float g, float b, float* h, float* s, float* v);

float DeltaE00(float l1, float a1, float b1, float l2, float a2, float b2);
float DeltaE94(float l1, float a1, float b1, float l2, float a2, float b2);
float DeltaE76(float l1, float a1, float b1, float l2, float a2, float b2);

#endif /* slic3r_ColorSpaceConvert_hpp_ */
