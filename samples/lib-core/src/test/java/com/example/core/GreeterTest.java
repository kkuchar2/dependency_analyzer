package com.example.core;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

class GreeterTest {
    @Test
    void greets() {
        assertThat(new Greeter().greet("bob")).isEqualTo("Hello Bob");
    }
}
