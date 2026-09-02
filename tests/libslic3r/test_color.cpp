#include <catch2/catch_all.hpp>

#include "libslic3r/Color.hpp"

#include <array>

using namespace Slic3r;

TEST_CASE("Color decoder preserves exact RGB and RGBA bytes", "[Color]")
{
    std::array<unsigned char, 4> decoded;

    REQUIRE(decode_color("#009688", decoded));
    REQUIRE((decoded == std::array<unsigned char, 4>{0x00, 0x96, 0x88, 0xff}));

    REQUIRE(decode_color("#12abEF34", decoded));
    REQUIRE((decoded == std::array<unsigned char, 4>{0x12, 0xab, 0xef, 0x34}));
}

TEST_CASE("Color decoder rejects malformed hexadecimal components", "[Color]")
{
    std::array<unsigned char, 4> decoded {1, 2, 3, 4};
    REQUIRE_FALSE(decode_color("#12xxEF", decoded));
    REQUIRE((decoded == std::array<unsigned char, 4>{0, 0, 0, 255}));

    ColorRGBA rgba = ColorRGBA::WHITE();
    REQUIRE_FALSE(decode_color("#12345z78", rgba));
    REQUIRE(rgba == ColorRGBA::BLACK());
}
