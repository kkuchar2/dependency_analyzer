package com.example.utils;

import com.example.core.Greeter;
import com.google.gson.Gson;

public class Json {
    private final Gson gson = new Gson();

    public String greetingJson(String name) {
        return gson.toJson(new Greeter().greet(name));
    }
}
