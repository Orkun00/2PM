//
// Created by Orkun Acar on 19.08.2026.
//

#include "galvo_controller.h"

#include <cmath>

bool Galvo::set_voltage_xy(double input_voltage_x, double input_voltage_y) {
    if (!std::isfinite(input_voltage_x) || !std::isfinite(input_voltage_y) || std::abs(input_voltage_x) > VOLTAGE_LIMIT || std::abs(input_voltage_y) > VOLTAGE_LIMIT) {
        return false;
    }
    voltage_x = input_voltage_x;
    voltage_y = input_voltage_y;
    return true;
}

bool Galvo::set_voltage_x(double input_voltage_x) {
    if (!std::isfinite(input_voltage_x) || std::abs(input_voltage_x) > VOLTAGE_LIMIT) {
        return false;
    }
    voltage_x = input_voltage_x;
    return true;
}

bool Galvo::set_voltage_y(double input_voltage_y) {
    if (!std::isfinite(input_voltage_y) || std::abs(input_voltage_y) > VOLTAGE_LIMIT) {
        return false;
    }
    voltage_y = input_voltage_y;
    return true;
}

double Galvo::get_voltage_x() const {
    return voltage_x;
}

double Galvo::get_voltage_y() const {
    return voltage_y;
}

double Galvo::get_degree_x() const {
    return voltage_to_degree(voltage_x);
}

double Galvo::get_degree_y() const {
    return voltage_to_degree(voltage_y);
}


