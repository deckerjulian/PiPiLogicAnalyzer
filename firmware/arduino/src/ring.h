// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// A ring of fixed-size records: one producer (an interrupt), one consumer (the main loop).

#ifndef RING_H
#define RING_H

#include "hal.h"

class Ring {
public:
    void init(uint8_t *memory, uint32_t bytes, uint16_t record) {
        memory_ = memory;
        record_ = record ? record : 1;
        uint32_t capacity = bytes / record_;
        capacity_ = (uint16_t)(capacity > 0xFFFF ? 0xFFFF : capacity);
        head_ = tail_ = 0;
    }
    uint16_t record() const { return record_; }
    // records it can hold at once
    uint16_t capacity() const { return capacity_ ? (uint16_t)(capacity_ - 1) : 0; }

    // producer: the next free record or nullptr when full; commit() publishes it
    uint8_t *slot() const {
        if (capacity_ < 2) return nullptr;
        uint16_t next = advance(head_);
        if (next == tail_) return nullptr;
        return memory_ + (uint32_t)head_ * record_;
    }
    void commit() { head_ = advance(head_); }

    // consumer
    uint16_t count() const {
        uint32_t state = hal::irq_save();
        uint16_t head = head_;
        hal::irq_restore(state);
        return head >= tail_ ? (uint16_t)(head - tail_) : (uint16_t)(capacity_ - tail_ + head);
    }
    const uint8_t *front() const { return memory_ + (uint32_t)tail_ * record_; }
    void pop() {
        uint16_t tail = advance(tail_);
        uint32_t state = hal::irq_save();  // 16 bit stores are not atomic on AVR
        tail_ = tail;
        hal::irq_restore(state);
    }
    void clear() { head_ = tail_ = 0; }

private:
    uint16_t advance(uint16_t index) const { return (uint16_t)(index + 1 == capacity_ ? 0 : index + 1); }

    uint8_t *memory_ = nullptr;
    uint16_t record_ = 1;
    uint16_t capacity_ = 0;
    volatile uint16_t head_ = 0;
    volatile uint16_t tail_ = 0;
};

#endif
