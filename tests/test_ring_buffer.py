from audio.ring_buffer import RingBuffer


def test_ring_buffer_drops_oldest_and_drains_in_order() -> None:
    buffer = RingBuffer[int](capacity=3)

    buffer.append(1)
    buffer.append(2)
    buffer.append(3)
    buffer.append(4)

    assert buffer.drain() == [2, 3, 4]
    assert len(buffer) == 0
