use jni::objects::{JClass, JString};
use jni::sys::{jboolean, jint, jintArray};
use jni::JNIEnv;
use std::ptr;
use std::sync::OnceLock;
use tokenizers::Tokenizer;

static BGE: OnceLock<(String, Tokenizer)> = OnceLock::new();
static FLASH: OnceLock<(String, Tokenizer)> = OnceLock::new();

fn read_string(env: &mut JNIEnv<'_>, input: &JString<'_>) -> Result<String, String> {
    env.get_string(input)
        .map(Into::into)
        .map_err(|err| err.to_string())
}

fn open(slot: &OnceLock<(String, Tokenizer)>, path: String) -> Result<(), String> {
    if slot.get().is_none() {
        let mut tokenizer = Tokenizer::from_file(&path).map_err(|err| err.to_string())?;
        tokenizer.with_padding(None);
        tokenizer.with_truncation(None).map_err(|err| err.to_string())?;
        let _ = slot.set((path.clone(), tokenizer));
    }
    if slot.get().is_some_and(|(current, _)| current == &path) {
        Ok(())
    } else {
        Err("Tokenizer already bound to a different asset".into())
    }
}

#[no_mangle]
pub extern "system" fn Java_io_github_foxmoreland_pcar_runtimecheck_NativeTokenizers_open(
    mut env: JNIEnv<'_>,
    _class: JClass<'_>,
    which: jint,
    path: JString<'_>,
) {
    let result = read_string(&mut env, &path).and_then(|path| match which {
        0 => open(&BGE, path),
        1 => open(&FLASH, path),
        _ => Err("Unknown tokenizer".into()),
    });
    if let Err(reason) = result {
        let _ = env.throw_new("java/lang/IllegalStateException", reason);
    }
}

#[no_mangle]
pub extern "system" fn Java_io_github_foxmoreland_pcar_runtimecheck_NativeTokenizers_encodeBge(
    mut env: JNIEnv<'_>,
    _class: JClass<'_>,
    value: JString<'_>,
    special: jboolean,
) -> jintArray {
    let result = read_string(&mut env, &value).and_then(|text| {
        let (_, tokenizer) = BGE.get().ok_or("BGE tokenizer is unopened")?;
        let result = tokenizer
            .encode_char_offsets(text, special != 0)
            .map_err(|err| err.to_string())?;
        let values: Vec<jint> = result
            .get_ids()
            .iter()
            .zip(result.get_offsets())
            .flat_map(|(&id, &(start, end))| [id as jint, start as jint, end as jint])
            .collect();
        let length = jint::try_from(values.len()).map_err(|_| "BGE token output too large")?;
        let output = env.new_int_array(length).map_err(|err| err.to_string())?;
        env.set_int_array_region(&output, 0, &values)
            .map_err(|err| err.to_string())?;
        Ok(output.into_raw())
    });
    match result {
        Ok(value) => value,
        Err(reason) => {
            let _ = env.throw_new("java/lang/IllegalStateException", reason);
            ptr::null_mut()
        }
    }
}

#[no_mangle]
pub extern "system" fn Java_io_github_foxmoreland_pcar_runtimecheck_NativeTokenizers_countFlash(
    mut env: JNIEnv<'_>,
    _class: JClass<'_>,
    value: JString<'_>,
) -> jint {
    let result = read_string(&mut env, &value).and_then(|text| {
        let (_, tokenizer) = FLASH.get().ok_or("Flash tokenizer is unopened")?;
        let result = tokenizer.encode(text, false).map_err(|err| err.to_string())?;
        jint::try_from(result.len()).map_err(|_| "Flash token count too large".into())
    });
    match result {
        Ok(count) => count,
        Err(reason) => {
            let _ = env.throw_new("java/lang/IllegalStateException", reason);
            -1
        }
    }
}
