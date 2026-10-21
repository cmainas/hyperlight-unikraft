// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Hyperlight Authors.

//! The ClickHouse types a function takes and returns, and the two forms
//! their values take: RowBinary on ClickHouse's pipe, JSON in the guest.
//!
//! Arguments go one way only, RowBinary to JSON, and results the other,
//! so each direction is a single pass with no tree in between: the
//! guest's results, which are not trusted, cost the host no more than the
//! bytes they encode to.

use std::fmt;
use std::io::{self, BufRead, Read};

use serde::de::{self, DeserializeSeed, Deserializer, SeqAccess, Visitor};

/// A type a function may take or return.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Type {
    Bool,
    /// `UInt8` to `UInt64`, by width in bytes.
    UInt(u8),
    /// `Int8` to `Int64`, by width in bytes.
    Int(u8),
    Float32,
    Float64,
    String,
    Nullable(Box<Type>),
    Array(Box<Type>),
}

impl Type {
    /// Parse a type as ClickHouse writes it in a signature.  `LowCardinality`
    /// is accepted and dropped: RowBinary carries its values as plain ones.
    pub fn parse(text: &str) -> Result<Type, String> {
        let text = text.trim();
        if let Some(inner) = wrapped(text, "Nullable") {
            let parsed = Type::parse(inner)?;
            // As ClickHouse refuses them, rather than once its configuration is loaded, past a guest's boot
            if matches!(parsed, Type::Array(_) | Type::Nullable(_))
                || inner.trim_start().starts_with("LowCardinality")
            {
                return Err(format!(
                    "{text}: ClickHouse has no Nullable of an Array, a Nullable or a LowCardinality"
                ));
            }
            return Ok(Type::Nullable(Box::new(parsed)));
        }
        if let Some(inner) = wrapped(text, "Array") {
            return Ok(Type::Array(Box::new(Type::parse(inner)?)));
        }
        if let Some(inner) = wrapped(text, "LowCardinality") {
            return Type::parse(inner);
        }
        Ok(match text {
            "Bool" => Type::Bool,
            "UInt8" => Type::UInt(1),
            "UInt16" => Type::UInt(2),
            "UInt32" => Type::UInt(4),
            "UInt64" => Type::UInt(8),
            "Int8" => Type::Int(1),
            "Int16" => Type::Int(2),
            "Int32" => Type::Int(4),
            "Int64" => Type::Int(8),
            "Float32" => Type::Float32,
            "Float64" => Type::Float64,
            "String" => Type::String,
            _ => {
                return sql_alias(text).ok_or_else(|| {
                    format!(
                        "unsupported type {text}: use Bool, (U)Int8 to (U)Int64, Float32, Float64 or String, \
                         or Nullable or Array of them"
                    )
                });
            }
        })
    }

    /// How the guest is told the type: its name, or `[wrapper, inner]`.
    pub fn descriptor(&self) -> serde_json::Value {
        match self {
            Type::Nullable(inner) => serde_json::json!(["Nullable", inner.descriptor()]),
            Type::Array(inner) => serde_json::json!(["Array", inner.descriptor()]),
            scalar => serde_json::Value::String(scalar.to_string()),
        }
    }

    /// Read one value in RowBinary and write it as JSON.  A float that is
    /// not finite, which JSON has no number for, is written as the string
    /// `"nan"`, `"inf"` or `"-inf"`.
    /// `text` holds a string's bytes on their way, reused from one to the
    /// next.
    pub fn rowbinary_to_json(
        &self,
        input: &mut impl BufRead,
        out: &mut Vec<u8>,
        text: &mut Vec<u8>,
    ) -> io::Result<()> {
        match self {
            Type::Bool => out.extend_from_slice(if read_array::<1>(input)?[0] != 0 {
                b"true"
            } else {
                b"false"
            }),
            Type::UInt(width) => {
                let mut bytes = [0; 8];
                input.read_exact(&mut bytes[..*width as usize])?;
                serde_json::to_writer(&mut *out, &u64::from_le_bytes(bytes))?;
            }
            Type::Int(width) => {
                let width = *width as usize;
                let mut bytes = [0; 8];
                input.read_exact(&mut bytes[..width])?;
                // One read for all four widths: the narrow ones' sign bit moved to i64's, and back
                let shift = 64 - 8 * width as u32;
                serde_json::to_writer(&mut *out, &((i64::from_le_bytes(bytes) << shift) >> shift))?;
            }
            Type::Float32 => write_float(f32::from_le_bytes(read_array(input)?).into(), out)?,
            Type::Float64 => write_float(f64::from_le_bytes(read_array(input)?), out)?,
            Type::String => {
                let len = read_varint(input)?;
                text.clear();
                // Grown as the bytes come, rather than to a length a broken stream could make up
                input.take(len).read_to_end(text)?;
                if text.len() as u64 != len {
                    return Err(io::ErrorKind::UnexpectedEof.into());
                }
                let text = std::str::from_utf8(text).map_err(|_| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "a String argument is not valid UTF-8",
                    )
                })?;
                serde_json::to_writer(&mut *out, text)?;
            }
            Type::Nullable(inner) => {
                if read_array::<1>(input)?[0] != 0 {
                    out.extend_from_slice(b"null");
                } else {
                    inner.rowbinary_to_json(input, out, text)?;
                }
            }
            Type::Array(inner) => {
                let len = read_varint(input)?;
                out.push(b'[');
                for i in 0..len {
                    if i > 0 {
                        out.push(b',');
                    }
                    inner.rowbinary_to_json(input, out, text)?;
                }
                out.push(b']');
            }
        }
        Ok(())
    }
}

impl fmt::Display for Type {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Type::Bool => f.write_str("Bool"),
            Type::UInt(width) => write!(f, "UInt{}", width * 8),
            Type::Int(width) => write!(f, "Int{}", width * 8),
            Type::Float32 => f.write_str("Float32"),
            Type::Float64 => f.write_str("Float64"),
            Type::String => f.write_str("String"),
            Type::Nullable(inner) => write!(f, "Nullable({inner})"),
            Type::Array(inner) => write!(f, "Array({inner})"),
        }
    }
}

/// A SQL name ClickHouse takes for one of the types, in any case, as
/// ClickHouse resolves it: `TEXT`, `VARCHAR(255)`, `BIGINT UNSIGNED`,
/// `DOUBLE PRECISION`.  ClickHouse passes a signature on as written.
fn sql_alias(text: &str) -> Option<Type> {
    let upper = text.to_ascii_uppercase();
    let mut words = upper.split_whitespace();
    let first = words.next()?;
    // A length or display width, which ClickHouse ignores: VARCHAR(255), INT(11)
    let base = match first.split_once('(') {
        Some((base, width)) => {
            let width = width.strip_suffix(')')?;
            (!width.is_empty() && width.bytes().all(|b| b.is_ascii_digit())).then_some(base)?
        }
        None => first,
    };
    let rest: Vec<&str> = words.collect();
    let width = match base {
        "TINYINT" | "INT1" | "BYTE" => 1,
        "SMALLINT" => 2,
        "INT" | "INTEGER" | "MEDIUMINT" => 4,
        "BIGINT" => 8,
        _ => {
            return match (base, rest.as_slice()) {
                (
                    "TEXT" | "VARCHAR" | "VARCHAR2" | "CHAR" | "CHARACTER" | "NCHAR" | "NVARCHAR"
                    | "CLOB" | "BLOB" | "TINYBLOB" | "MEDIUMBLOB" | "LONGBLOB" | "TINYTEXT"
                    | "MEDIUMTEXT" | "LONGTEXT" | "BYTEA" | "VARBINARY",
                    [],
                )
                | ("CHAR" | "CHARACTER" | "BINARY", ["VARYING"])
                | ("NATIONAL", ["CHAR"]) => Some(Type::String),
                ("FLOAT" | "REAL" | "SINGLE", []) => Some(Type::Float32),
                ("DOUBLE", [] | ["PRECISION"]) => Some(Type::Float64),
                ("BOOL" | "BOOLEAN", []) => Some(Type::Bool),
                // MySQL's, which ClickHouse takes as these
                ("YEAR", []) => Some(Type::UInt(2)),
                ("SET" | "BIT", []) => Some(Type::UInt(8)),
                _ => None,
            };
        }
    };
    match rest.as_slice() {
        [] | ["SIGNED"] => Some(Type::Int(width)),
        ["UNSIGNED"] => Some(Type::UInt(width)),
        _ => None,
    }
}

/// `inner` of `name(inner)`.
fn wrapped<'a>(text: &'a str, name: &str) -> Option<&'a str> {
    text.strip_prefix(name)?
        .trim_start()
        .strip_prefix('(')?
        .strip_suffix(')')
}

/// Split a signature, `x UInt8, y Array(String)`, into names and types.
pub fn parse_signature(signature: &str) -> Result<Vec<(String, Type)>, String> {
    let mut arguments = Vec::new();
    let mut depth = 0usize;
    let mut start = 0;
    let mut quoted = false;
    // Only the commas outside parentheses and backquoted names separate arguments
    for (i, c) in signature.char_indices().chain([(signature.len(), ',')]) {
        match c {
            '`' => quoted = !quoted,
            _ if quoted => {}
            '(' => depth += 1,
            ')' => {
                depth = depth
                    .checked_sub(1)
                    .ok_or("unbalanced parentheses in the arguments")?
            }
            ',' if depth == 0 => {
                let part = signature[start..i].trim();
                start = i + 1;
                if part.is_empty() {
                    continue;
                }
                let not_a_pair = || format!("argument {part:?} is not a name and a type");
                // A backquoted name ends at its closing backquote, spaces and all
                let (name, ty) = match part.strip_prefix('`') {
                    Some(rest) => rest.split_once('`').ok_or_else(not_a_pair)?,
                    None => part
                        .split_once(char::is_whitespace)
                        .ok_or_else(not_a_pair)?,
                };
                if ty.trim().is_empty() {
                    return Err(not_a_pair());
                }
                arguments.push((name.to_string(), Type::parse(ty)?));
            }
            _ => {}
        }
    }
    if depth != 0 || quoted {
        return Err("unbalanced parentheses or backquotes in the arguments".into());
    }
    Ok(arguments)
}

fn read_array<const N: usize>(input: &mut impl Read) -> io::Result<[u8; N]> {
    let mut bytes = [0; N];
    input.read_exact(&mut bytes)?;
    Ok(bytes)
}

/// An unsigned LEB128, as RowBinary prefixes strings and arrays with.
fn read_varint(input: &mut impl Read) -> io::Result<u64> {
    let mut value = 0u64;
    for shift in (0..64).step_by(7) {
        let [byte] = read_array(input)?;
        // The tenth byte holds the one bit left
        if shift == 63 && byte > 1 {
            break;
        }
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "a length longer than 64 bits",
    ))
}

fn write_varint(mut value: u64, out: &mut Vec<u8>) {
    while value >= 0x80 {
        out.push(value as u8 | 0x80);
        value >>= 7;
    }
    out.push(value as u8);
}

/// Shortest text that reads back as the same double: a Float32 widened as
/// ClickHouse widens it (`toFloat64`), so that Python's float is exactly
/// the value ClickHouse holds.
fn write_float(value: f64, out: &mut Vec<u8>) -> io::Result<()> {
    if value.is_nan() {
        out.extend_from_slice(b"\"nan\"");
    } else if value.is_infinite() {
        out.extend_from_slice(if value > 0.0 { b"\"inf\"" } else { b"\"-inf\"" });
    } else {
        serde_json::to_writer(&mut *out, &value)?;
    }
    Ok(())
}

/// Encode the guest's results, a JSON array of exactly `rows` values of
/// `ty`, in RowBinary, in at most `limit` bytes: an array of numbers takes
/// several times the bytes of its JSON.
pub fn json_results_to_rowbinary(
    json: &str,
    ty: &Type,
    rows: usize,
    limit: usize,
    out: &mut Vec<u8>,
) -> Result<(), String> {
    let mut deserializer = serde_json::Deserializer::from_str(json);
    deserializer
        .deserialize_seq(Results {
            ty,
            rows,
            limit,
            out,
        })
        .and_then(|()| deserializer.end())
        .map_err(|e| {
            // Without serde_json's place in the JSON, which tells a user nothing
            let message = e.to_string();
            let place = format!(" at line {} column {}", e.line(), e.column());
            message.strip_suffix(&place).unwrap_or(&message).to_string()
        })
}

struct Results<'a> {
    ty: &'a Type,
    rows: usize,
    limit: usize,
    out: &'a mut Vec<u8>,
}

impl<'de> Visitor<'de> for Results<'_> {
    type Value = ();

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "an array of {} results", self.rows)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        let mut count = 0;
        while count < self.rows {
            let row = seq
                .next_element_seed(Encode {
                    ty: self.ty,
                    // What is left of it: an array's items are encoded aside, before they join the rows
                    limit: self.limit.saturating_sub(self.out.len()),
                    out: &mut *self.out,
                })
                .map_err(|e| {
                    de::Error::custom(format!("the result on row {} of the block: {e}", count + 1))
                })?;
            if row.is_none() {
                break;
            }
            if self.out.len() > self.limit {
                return Err(de::Error::custom(format!(
                    "row {} of the block: the results take more than {} bytes",
                    count + 1,
                    self.limit
                )));
            }
            count += 1;
        }
        // One more would be past what was asked for: refused before it is read
        if count < self.rows || seq.next_element::<de::IgnoredAny>()?.is_some() {
            return Err(de::Error::custom(format!(
                "{} results where the block has {} rows",
                if count < self.rows {
                    count.to_string()
                } else {
                    "more".into()
                },
                self.rows
            )));
        }
        Ok(())
    }
}

/// One value of `ty`, written in RowBinary as it is read.
struct Encode<'a> {
    ty: &'a Type,
    out: &'a mut Vec<u8>,
    /// The most an array's items may take, encoded aside before their count
    limit: usize,
}

impl<'de> DeserializeSeed<'de> for Encode<'_> {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        match self.ty {
            // null, or a value of the inner type: visit_none or visit_some
            Type::Nullable(_) => deserializer.deserialize_option(self),
            _ => deserializer.deserialize_any(self),
        }
    }
}

impl Encode<'_> {
    fn integer<E: de::Error>(self, value: i128) -> Result<(), E> {
        let (min, max, width) = match self.ty {
            Type::UInt(width) => (0, (1i128 << (8 * width)) - 1, *width),
            Type::Int(width) => (
                -(1i128 << (8 * width - 1)),
                (1i128 << (8 * width - 1)) - 1,
                *width,
            ),
            Type::Float32 | Type::Float64 => return self.float(value as f64),
            // As ClickHouse reads a Bool: 0 or 1
            Type::Bool if value == 0 || value == 1 => {
                self.out.push(value as u8);
                return Ok(());
            }
            _ => return Err(self.mismatch("an integer")),
        };
        if !(min..=max).contains(&value) {
            return Err(E::custom(format!(
                "{value} is out of range for {}",
                self.ty
            )));
        }
        self.out
            .extend_from_slice(&value.to_le_bytes()[..width as usize]);
        Ok(())
    }

    fn float<E: de::Error>(self, value: f64) -> Result<(), E> {
        match self.ty {
            Type::Float32 => {
                let narrow = value as f32;
                // Past f32's range, `as` makes an infinity of a finite value
                if narrow.is_infinite() && value.is_finite() {
                    return Err(E::custom(format!("{value} is out of range for Float32")));
                }
                self.out.extend_from_slice(&narrow.to_le_bytes());
            }
            Type::Float64 => self.out.extend_from_slice(&value.to_le_bytes()),
            // A float, or an integer past 64 bits, which JSON hands over as one
            Type::UInt(_) | Type::Int(_) => {
                return Err(E::custom(format!(
                    "a float ({value:?}), or an integer past 64 bits, where the function returns {}: \
                     return an int",
                    self.ty
                )));
            }
            _ => return Err(self.mismatch("a float")),
        }
        Ok(())
    }

    fn mismatch<E: de::Error>(&self, found: &str) -> E {
        E::custom(format!("{found} where the function returns {}", self.ty))
    }
}

impl<'de> Visitor<'de> for Encode<'_> {
    type Value = ();

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "a value of type {}", self.ty)
    }

    fn visit_bool<E: de::Error>(self, value: bool) -> Result<(), E> {
        match self.ty {
            Type::Bool => self.out.push(value as u8),
            // A Python bool is an int too, as ClickHouse's own booleans often are (UInt8)
            Type::UInt(_) | Type::Int(_) | Type::Float32 | Type::Float64 => {
                return self.integer(value.into());
            }
            _ => return Err(self.mismatch("a boolean")),
        }
        Ok(())
    }

    fn visit_i64<E: de::Error>(self, value: i64) -> Result<(), E> {
        self.integer(value.into())
    }

    fn visit_u64<E: de::Error>(self, value: u64) -> Result<(), E> {
        self.integer(value.into())
    }

    fn visit_f64<E: de::Error>(self, value: f64) -> Result<(), E> {
        self.float(value)
    }

    fn visit_str<E: de::Error>(self, value: &str) -> Result<(), E> {
        match self.ty {
            Type::String => {
                write_varint(value.len() as u64, self.out);
                self.out.extend_from_slice(value.as_bytes());
            }
            // The guest's form of the floats that JSON has no number for
            Type::Float32 | Type::Float64 => {
                let special = match value {
                    "nan" => f64::NAN,
                    "inf" => f64::INFINITY,
                    "-inf" => f64::NEG_INFINITY,
                    _ => return Err(self.mismatch("a string")),
                };
                return self.float(special);
            }
            _ => return Err(self.mismatch("a string")),
        }
        Ok(())
    }

    // A null where the type is not Nullable, which deserialize_any reads as a unit
    fn visit_unit<E: de::Error>(self) -> Result<(), E> {
        Err(self.mismatch("None"))
    }

    fn visit_none<E: de::Error>(self) -> Result<(), E> {
        self.out.push(1);
        Ok(())
    }

    fn visit_some<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        let Type::Nullable(inner) = self.ty else {
            return Err(self.mismatch("a value"));
        };
        self.out.push(0);
        Encode {
            ty: inner,
            out: self.out,
            limit: self.limit,
        }
        .deserialize(deserializer)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        let inner = match self.ty {
            Type::Array(inner) => inner,
            _ => return Err(self.mismatch("a list")),
        };
        // RowBinary counts the items before them, so they are encoded aside first
        let mut items = Vec::new();
        let mut count = 0u64;
        while seq
            .next_element_seed(Encode {
                ty: inner,
                // What is left of it, as the items before take their share
                limit: self.limit.saturating_sub(items.len()),
                out: &mut items,
            })?
            .is_some()
        {
            if items.len() > self.limit {
                // What is left of the limit is all this array knows of it
                return Err(de::Error::custom(
                    "the results grow past the limit of a block's",
                ));
            }
            count += 1;
        }
        write_varint(count, self.out);
        self.out.extend_from_slice(&items);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn to_json(ty: &str, rowbinary: &[u8]) -> String {
        let mut out = Vec::new();
        Type::parse(ty)
            .unwrap()
            .rowbinary_to_json(&mut &rowbinary[..], &mut out, &mut Vec::new())
            .unwrap();
        String::from_utf8(out).unwrap()
    }

    fn to_rowbinary(ty: &str, json: &str, rows: usize) -> Result<Vec<u8>, String> {
        let mut out = Vec::new();
        json_results_to_rowbinary(json, &Type::parse(ty).unwrap(), rows, 64, &mut out)?;
        Ok(out)
    }

    #[test]
    fn parses_signatures() {
        let parsed =
            parse_signature("x UInt8, `y` Array(Nullable(String)), z LowCardinality(String)")
                .unwrap();
        assert_eq!(
            parsed,
            [
                ("x".into(), Type::UInt(1)),
                (
                    "y".into(),
                    Type::Array(Box::new(Type::Nullable(Box::new(Type::String))))
                ),
                ("z".into(), Type::String),
            ]
        );
        assert_eq!(parse_signature("").unwrap(), []);
        assert!(
            parse_signature("x Decimal(10, 2)")
                .unwrap_err()
                .contains("unsupported type Decimal(10, 2)")
        );
        assert!(parse_signature("x Array(String").is_err());
        assert!(parse_signature("UInt8").is_err());
        assert_eq!(
            parse_signature("`my arg, (1)` String").unwrap(),
            [("my arg, (1)".into(), Type::String)]
        );
        assert!(parse_signature("`x String").is_err());
        assert!(parse_signature("`x`").is_err());
    }

    #[test]
    fn parses_sql_aliases_as_clickhouse_does() {
        // What `toTypeName(defaultValueOfTypeName(alias))` says on ClickHouse 26.9
        for (alias, ty) in [
            ("text", "String"),
            ("VARCHAR(255)", "String"),
            ("LONGTEXT", "String"),
            ("TINYINT", "Int8"),
            ("SMALLINT SIGNED", "Int16"),
            ("INT(11)", "Int32"),
            ("integer", "Int32"),
            ("INT UNSIGNED", "UInt32"),
            ("BIGINT UNSIGNED", "UInt64"),
            ("FLOAT", "Float32"),
            ("double precision", "Float64"),
            ("BOOLEAN", "Bool"),
            ("Array(BIGINT)", "Array(Int64)"),
            ("LowCardinality(TEXT)", "String"),
            ("VARBINARY", "String"),
            ("character varying", "String"),
            ("NATIONAL CHAR", "String"),
            ("LONGBLOB", "String"),
            ("INT1", "Int8"),
            ("BYTE", "Int8"),
            ("INTEGER UNSIGNED", "UInt32"),
            ("MEDIUMINT UNSIGNED", "UInt32"),
            ("SINGLE", "Float32"),
            ("YEAR", "UInt16"),
            ("BIT", "UInt64"),
        ] {
            assert_eq!(Type::parse(alias).unwrap().to_string(), ty, "{alias}");
        }
        // Which ClickHouse refuses too: its own names are case-sensitive, INT8 is not Int8, and BINARY needs a
        // length (FixedString)
        for refused in [
            "string",
            "uint8",
            "INT8",
            "INT2",
            "BINARY",
            "nullable(String)",
            "VARCHAR(x)",
            "INT FOO",
            "Nullable(Array(String))",
            "Nullable(Nullable(UInt8))",
            "Nullable(LowCardinality(String))",
        ] {
            assert!(Type::parse(refused).is_err(), "{refused}");
        }
    }

    #[test]
    fn reads_rowbinary_as_json() {
        assert_eq!(
            to_json("UInt64", &u64::MAX.to_le_bytes()),
            u64::MAX.to_string()
        );
        assert_eq!(to_json("Int8", &[0xff]), "-1");
        assert_eq!(to_json("Int32", &(-5i32).to_le_bytes()), "-5");
        assert_eq!(
            to_json("Int64", &i64::MIN.to_le_bytes()),
            i64::MIN.to_string()
        );
        assert_eq!(
            to_json("Float32", &0.1f32.to_le_bytes()),
            "0.10000000149011612"
        );
        assert_eq!(to_json("Float64", &f64::NAN.to_le_bytes()), "\"nan\"");
        assert_eq!(
            to_json("Float64", &f64::NEG_INFINITY.to_le_bytes()),
            "\"-inf\""
        );
        assert_eq!(to_json("Bool", &[1]), "true");
        assert_eq!(to_json("String", b"\x04a\"\xc3\xa9"), "\"a\\\"é\"");
        assert_eq!(to_json("Nullable(String)", &[1]), "null");
        assert_eq!(
            to_json("Array(Nullable(UInt8))", &[3, 0, 7, 1, 0, 9]),
            "[7,null,9]"
        );
        let mut longest = vec![0xff; 9];
        longest.push(0x01);
        assert_eq!(read_varint(&mut &longest[..]).unwrap(), u64::MAX);
        let mut past = vec![0xff; 9];
        past.push(0x02);
        assert!(read_varint(&mut &past[..]).is_err());
    }

    #[test]
    fn refuses_strings_that_are_not_utf8() {
        let mut out = Vec::new();
        let error = Type::String
            .rowbinary_to_json(&mut &b"\x01\xff"[..], &mut out, &mut Vec::new())
            .unwrap_err();
        assert!(error.to_string().contains("not valid UTF-8"));
    }

    #[test]
    fn encodes_results() {
        assert_eq!(
            to_rowbinary("UInt16", "[1, 65535]", 2).unwrap(),
            [1, 0, 255, 255]
        );
        assert_eq!(to_rowbinary("Int8", "[-128]", 1).unwrap(), [0x80]);
        assert_eq!(
            to_rowbinary("Float32", "[1]", 1).unwrap(),
            1f32.to_le_bytes()
        );
        assert_eq!(
            to_rowbinary("Float64", "[\"-inf\"]", 1).unwrap(),
            f64::NEG_INFINITY.to_le_bytes()
        );
        assert_eq!(
            to_rowbinary("String", "[\"é\"]", 1).unwrap(),
            b"\x02\xc3\xa9"
        );
        assert_eq!(
            to_rowbinary("Nullable(Bool)", "[null, true]", 2).unwrap(),
            [1, 0, 1]
        );
        assert_eq!(to_rowbinary("Bool", "[0, 1, false]", 3).unwrap(), [0, 1, 0]);
        assert_eq!(to_rowbinary("UInt8", "[true, false]", 2).unwrap(), [1, 0]);
        assert_eq!(
            to_rowbinary("Array(Array(UInt8))", "[[[1], []]]", 1).unwrap(),
            [2, 1, 1, 0]
        );
    }

    #[test]
    fn refuses_results_that_do_not_fit() {
        let error = |ty, json, rows| to_rowbinary(ty, json, rows).unwrap_err();
        assert!(
            error("UInt8", "[256]", 1)
                .contains("row 1 of the block: 256 is out of range for UInt8")
        );
        assert!(
            error("Bool", "[1, 2]", 2)
                .contains("row 2 of the block: an integer where the function returns Bool")
        );
        assert!(error("UInt64", "[-1]", 1).contains("out of range"));
        assert!(error("Int64", "[1.5]", 1).contains(
            "a float (1.5), or an integer past 64 bits, where the function returns Int64"
        ));
        assert!(error("Int64", "[1180591620717411303424]", 1).contains("an integer past 64 bits"));
        assert!(error("Float32", "[1e39]", 1).contains("out of range for Float32"));
        assert!(error("String", "[1]", 1).contains("an integer where the function returns String"));
        assert!(error("UInt8", "[null]", 1).contains("None where the function returns UInt8"));
        assert!(error("Float64", "[\"NaN\"]", 1).contains("a string where"));
        assert!(error("UInt8", "[1]", 2).contains("1 results where the block has 2 rows"));
        assert!(error("UInt8", "[1, 2]", 1).contains("more results where the block has 1 rows"));
        assert!(error("UInt8", "[1] x", 1).contains("trailing"));
        assert!(error("UInt8", "{}", 1).contains("an array of 1 results"));
        assert!(
            error("Array(UInt64)", "[[1, 2, 3, 4, 5, 6, 7, 8, 9]]", 1)
                .contains("the results grow past the limit")
        );
        // Bounded as the array's items are encoded, before the row is done
        assert!(
            error("Array(Array(UInt64))", "[[[1, 2, 3, 4, 5, 6, 7, 8, 9]]]", 1)
                .contains("the results grow past the limit")
        );
    }
}
