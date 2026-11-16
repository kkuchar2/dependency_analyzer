package com.example.app;

import com.example.utils.Json;

public class App {
    public static void main(String[] args) {
        System.out.println(new Json().greetingJson(args.length > 0 ? args[0] : "world"));
    }
}
