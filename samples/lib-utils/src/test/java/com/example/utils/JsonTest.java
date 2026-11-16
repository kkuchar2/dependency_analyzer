package com.example.utils;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;

import org.junit.jupiter.api.Test;

class JsonTest {
    @Test
    void json() {
        Runnable r = mock(Runnable.class);
        r.run();
        assertEquals("\"Hello Bob\"", new Json().greetingJson("bob"));
    }
}
