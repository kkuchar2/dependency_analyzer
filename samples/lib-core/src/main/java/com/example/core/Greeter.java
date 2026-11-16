package com.example.core;

import org.apache.commons.lang3.StringUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Greeter {
    private static final Logger LOG = LoggerFactory.getLogger(Greeter.class);

    public String greet(String name) {
        LOG.debug("greeting {}", name);
        return "Hello " + StringUtils.capitalize(name);
    }
}
