// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The frame layer against the shared test vectors (tests/fixtures/arduino_frames.json, the same
// the Python driver is tested with) and the decoder's handling of damaged frames.

#include <unity.h>

#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "protocol.h"

using namespace proto;

// ------------------------------------------------------------------ a small JSON reader

struct Json {
    enum Kind { NUL, NUMBER, STRING, ARRAY, OBJECT } kind = NUL;
    double number = 0;
    std::string text;
    std::vector<Json> items;
    std::map<std::string, Json> fields;
    const Json &operator[](const std::string &key) const { return fields.at(key); }
};

struct Reader {
    const std::string &s;
    size_t i = 0;
    explicit Reader(const std::string &source) : s(source) {}
    void space() {
        while (i < s.size() && isspace((unsigned char)s[i])) i++;
    }
    std::string string() {
        std::string out;
        i++;  // "
        while (s[i] != '"') {
            if (s[i] == '\\') i++;
            out += s[i++];
        }
        i++;
        return out;
    }
    Json value() {
        space();
        Json v;
        if (s[i] == '{') {
            v.kind = Json::OBJECT;
            i++;
            space();
            while (s[i] != '}') {
                space();
                std::string key = string();
                space();
                i++;  // :
                v.fields[key] = value();
                space();
                if (s[i] == ',') i++;
                space();
            }
            i++;
        } else if (s[i] == '[') {
            v.kind = Json::ARRAY;
            i++;
            space();
            while (s[i] != ']') {
                v.items.push_back(value());
                space();
                if (s[i] == ',') i++;
                space();
            }
            i++;
        } else if (s[i] == '"') {
            v.kind = Json::STRING;
            v.text = string();
        } else {
            v.kind = Json::NUMBER;
            size_t end = i;
            while (end < s.size() && (isdigit((unsigned char)s[end]) || s[end] == '-' || s[end] == '.')) end++;
            v.number = std::stod(s.substr(i, end - i));
            i = end;
        }
        return v;
    }
};

static Json fixtures;

static std::vector<uint8_t> hex(const std::string &text) {
    std::vector<uint8_t> out;
    for (size_t i = 0; i + 1 < text.size(); i += 2) out.push_back((uint8_t)std::stoul(text.substr(i, 2), nullptr, 16));
    return out;
}

static std::vector<uint8_t> sent;
static void sink(const uint8_t *data, size_t n) { sent.insert(sent.end(), data, data + n); }

// ------------------------------------------------------------------------------- tests

void test_fixtures_are_there() {
    TEST_ASSERT_TRUE(fixtures["crc8"].items.size() >= 4);
    TEST_ASSERT_TRUE(fixtures["cobs"].items.size() >= 6);
    TEST_ASSERT_TRUE(fixtures["frames"].items.size() >= 6);
}

void test_crc8_vectors() {
    for (const Json &v : fixtures["crc8"].items) {
        std::vector<uint8_t> data = hex(v["data"].text);
        TEST_ASSERT_EQUAL_UINT8((uint8_t)v["crc"].number, crc8(data.data(), data.size()));
    }
}

void test_cobs_vectors() {
    for (const Json &v : fixtures["cobs"].items) {
        std::vector<uint8_t> data = hex(v["data"].text), encoded = hex(v["encoded"].text);
        std::vector<uint8_t> out(data.size() + data.size() / 254 + 2);
        size_t n = cobs_encode(data.data(), data.size(), out.data());
        TEST_ASSERT_EQUAL_size_t(encoded.size(), n);
        TEST_ASSERT_EQUAL_MEMORY(encoded.data(), out.data(), n);
        std::vector<uint8_t> back(encoded.size() + 1);
        long m = cobs_decode(encoded.data(), encoded.size(), back.data());
        TEST_ASSERT_EQUAL_INT32((long)data.size(), m);
        if (m) TEST_ASSERT_EQUAL_MEMORY(data.data(), back.data(), (size_t)m);
        // in place, as the decoder does it
        std::vector<uint8_t> place = encoded;
        TEST_ASSERT_EQUAL_INT32((long)data.size(), cobs_decode(place.data(), place.size(), place.data()));
    }
}

void test_frames_are_written_as_the_vectors() {
    Writer writer;
    for (const Json &v : fixtures["frames"].items) {
        std::vector<uint8_t> payload = hex(v["payload"].text), wire = hex(v["wire"].text);
        sent.clear();
        writer.begin((uint8_t)v["type"].number, (uint8_t)v["sequence"].number);
        writer.bytes(payload.data(), payload.size());
        writer.send(sink);
        TEST_ASSERT_EQUAL_size_t(wire.size(), sent.size());
        TEST_ASSERT_EQUAL_MEMORY(wire.data(), sent.data(), wire.size());
    }
}

void test_frames_are_read_as_the_vectors() {
    Decoder decoder;
    for (const Json &v : fixtures["frames"].items) {
        std::vector<uint8_t> payload = hex(v["payload"].text), wire = hex(v["wire"].text);
        int frames = 0;
        for (uint8_t byte : wire) {
            if (decoder.feed(byte)) {
                frames++;
                const Frame &f = decoder.frame();
                TEST_ASSERT_EQUAL_UINT8((uint8_t)v["type"].number, f.type);
                TEST_ASSERT_EQUAL_UINT8((uint8_t)v["sequence"].number, f.sequence);
                TEST_ASSERT_EQUAL_UINT8(payload.size(), f.length);
                TEST_ASSERT_EQUAL_MEMORY(payload.data(), f.payload, payload.size());
            }
        }
        TEST_ASSERT_EQUAL_INT(1, frames);
    }
    TEST_ASSERT_EQUAL_UINT16(0, decoder.damaged());
}

static int feed(Decoder &decoder, const std::vector<uint8_t> &bytes) {
    int frames = 0;
    for (uint8_t byte : bytes) frames += decoder.feed(byte) ? 1 : 0;
    return frames;
}

void test_a_damaged_frame_is_counted_and_the_next_one_read() {
    Decoder decoder;
    std::vector<uint8_t> wire = hex(fixtures["frames"].items[1]["wire"].text);
    std::vector<uint8_t> broken = wire;
    broken[3] ^= 0x01;  // a flipped bit: the CRC does not fit
    TEST_ASSERT_EQUAL_INT(0, feed(decoder, broken));
    TEST_ASSERT_EQUAL_UINT16(1, decoder.damaged());
    TEST_ASSERT_EQUAL_INT(1, feed(decoder, wire));
    // a lost byte costs one frame
    std::vector<uint8_t> shorter(wire.begin() + 1, wire.end());
    TEST_ASSERT_EQUAL_INT(0, feed(decoder, shorter));
    TEST_ASSERT_EQUAL_UINT16(2, decoder.damaged());
    TEST_ASSERT_EQUAL_INT(1, feed(decoder, wire));
}

void test_empty_frames_are_ignored_and_frames_follow_each_other() {
    Decoder decoder;
    std::vector<uint8_t> a = hex(fixtures["frames"].items[0]["wire"].text);
    std::vector<uint8_t> b = hex(fixtures["frames"].items[2]["wire"].text);
    std::vector<uint8_t> line = {0, 0};
    line.insert(line.end(), a.begin(), a.end());
    line.push_back(0);
    line.insert(line.end(), b.begin(), b.end());
    TEST_ASSERT_EQUAL_INT(2, feed(decoder, line));
    TEST_ASSERT_EQUAL_UINT16(0, decoder.damaged());
}

void test_too_short_and_overlong_frames_are_damaged() {
    Decoder decoder;
    TEST_ASSERT_EQUAL_INT(0, feed(decoder, {0x03, 0x01, 0x02, 0x00}));  // two bytes: no CRC
    TEST_ASSERT_EQUAL_UINT16(1, decoder.damaged());
    std::vector<uint8_t> overlong(600, 0x11);
    overlong.push_back(0);
    TEST_ASSERT_EQUAL_INT(0, feed(decoder, overlong));
    TEST_ASSERT_EQUAL_UINT16(2, decoder.damaged());
    TEST_ASSERT_EQUAL_INT(1, feed(decoder, hex(fixtures["frames"].items[0]["wire"].text)));
    TEST_ASSERT_EQUAL_INT(0, feed(decoder, {0x05, 0x01, 0x00}));  // a block longer than the frame
    TEST_ASSERT_EQUAL_UINT16(3, decoder.damaged());
}

void test_the_largest_frame_goes_through() {
    Writer writer;
    Decoder decoder;
    writer.begin(T_EVENT, 42);
    std::vector<uint8_t> payload(MAX_PAYLOAD);
    for (size_t i = 0; i < payload.size(); i++) payload[i] = (uint8_t)(i % 7 ? i : 0);
    writer.bytes(payload.data(), payload.size());
    TEST_ASSERT_FALSE(writer.overflowed());
    writer.u8(1);
    TEST_ASSERT_TRUE(writer.overflowed());  // nothing beyond 250 bytes
    sent.clear();
    writer.send(sink);
    TEST_ASSERT_EQUAL_INT(1, feed(decoder, sent));
    TEST_ASSERT_EQUAL_UINT8(MAX_PAYLOAD, decoder.frame().length);
    TEST_ASSERT_EQUAL_MEMORY(payload.data(), decoder.frame().payload, payload.size());
}

void test_numbers_and_text() {
    Writer writer;
    writer.begin(T_ANSWER, 0);
    writer.u16(0x1234);
    writer.u32(0xA1B2C3D4);
    writer.f32(1.5f);
    writer.decimal(0);
    writer.decimal(4294967295u);
    writer.text("x");
    const uint8_t expected[] = {0x34, 0x12, 0xD4, 0xC3, 0xB2, 0xA1, 0x00, 0x00, 0xC0, 0x3F,
                                '0', '4', '2', '9', '4', '9', '6', '7', '2', '9', '5', 'x'};
    TEST_ASSERT_EQUAL_size_t(sizeof(expected), writer.payload_length());
    TEST_ASSERT_EQUAL_MEMORY(expected, writer.payload(), sizeof(expected));
}

void setUp() {}
void tearDown() {}

int main() {
    std::ifstream file(FIXTURES_PATH);
    std::stringstream text;
    text << file.rdbuf();
    std::string source = text.str();
    UNITY_BEGIN();
    if (source.empty()) {
        TEST_MESSAGE("cannot read " FIXTURES_PATH);
        UNITY_END();
        return 1;
    }
    Reader reader(source);
    fixtures = reader.value();
    RUN_TEST(test_fixtures_are_there);
    RUN_TEST(test_crc8_vectors);
    RUN_TEST(test_cobs_vectors);
    RUN_TEST(test_frames_are_written_as_the_vectors);
    RUN_TEST(test_frames_are_read_as_the_vectors);
    RUN_TEST(test_a_damaged_frame_is_counted_and_the_next_one_read);
    RUN_TEST(test_empty_frames_are_ignored_and_frames_follow_each_other);
    RUN_TEST(test_too_short_and_overlong_frames_are_damaged);
    RUN_TEST(test_the_largest_frame_goes_through);
    RUN_TEST(test_numbers_and_text);
    return UNITY_END();
}
